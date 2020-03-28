# Copyright (c) Ye Liu. All rights reserved.

import os.path as osp
from collections import OrderedDict
from datetime import timedelta

import torch
import torch.distributed as dist

import nncore
from ..comm import get_world_size, master_only
from .base import HOOKS, Hook

WRITERS = nncore.Registry('writer')


class Writer(object):

    def collect_metrics(self, engine, window_size):
        metrics = OrderedDict()

        metrics['mode'] = engine.mode
        metrics['epoch'] = engine.epoch
        metrics['iter'] = engine.iter_in_epoch

        if len(engine.optimizer.param_groups) == 1:
            metrics['lr'] = round(engine.optimizer.param_groups[0]['lr'], 5)
        else:
            metrics['lr'] = [
                round(group['lr'], 5)
                for group in engine.optimizer.param_groups
            ]

        if engine.mode == 'train':
            metrics['epoch'] += 1
            metrics['iter'] += 1
            metrics['time'] = engine.buffer.mean(
                '_iter_time', window_size=window_size)
            metrics['data_time'] = engine.buffer.mean(
                '_data_time', window_size=window_size)

        return metrics

    def open(self, engine):
        pass

    def close(self, engine):
        pass

    def write(self, engine, window_size):
        raise NotImplementedError


@WRITERS.register
class CommandLineWriter(Writer):

    _t_log = 'Epoch [{}][{}/{}] lr: {:.5f}, eta: {}, time: {:.3f}, data_time: {:.3f}, '  # noqa:E501
    _v_log = 'Epoch({}) [{}][{}] '

    def write(self, engine, window_size):
        metrics = self.collect_metrics(engine, window_size)

        if engine.mode == 'train':
            total_time = engine.buffer.latest('_total_time')
            num_iter_passed = engine.iter + 1 - engine.start_iter
            num_iter_left = engine.max_iters - engine.iter - 1
            eta = timedelta(
                seconds=int(num_iter_left * total_time / num_iter_passed))

            log = self._t_log.format(metrics['epoch'], metrics['iter'],
                                     len(engine.data_loader), metrics['lr'],
                                     eta, metrics['time'],
                                     metrics['data_time'])

            if torch.cuda.is_available():
                mem = torch.cuda.max_memory_allocated()
                mem_mb = torch.IntTensor([mem / (1024 * 1024)]).cuda()
                if get_world_size() > 1:
                    dist.reduce(mem_mb, 0, op=dist.ReduceOp.MAX)
                log += 'memory: {}, '.format(mem_mb.item())
        else:
            log = self._v_log.format(engine.mode, metrics['epoch'],
                                     len(engine.data_loader))

        ext = []
        for key in engine.buffer.keys():
            if key.startswith('_') or key.endswith('_'):
                continue

            if isinstance(engine.buffer.latest(key), dict):
                data = engine.buffer.avg(key, window_size=window_size)
                for k, v in data.items():
                    ext.append('{}: {:.4f}'.format('{}_{}'.format(key, k), v))
            else:
                ext.append('{}: {:.4f}'.format(
                    key, engine.buffer.avg(key, window_size=window_size)))

        log += ', '.join(ext)
        engine.logger.info(log)


@WRITERS.register
class JSONWriter(Writer):

    def __init__(self, filename='metrics.json'):
        """
        Args:
            filename (str, optional): name of the output JSON file
        """
        self._filename = filename

    def write(self, engine, window_size):
        metrics = self.collect_metrics(engine, window_size)

        for key in engine.buffer.keys():
            if key.startswith('_') or key.endswith('_'):
                continue

            if isinstance(engine.buffer.latest(key), dict):
                data = engine.buffer.avg(key, window_size=window_size)
                for k, v in data.items():
                    metrics['{}_{}'.format(key, k)] = v
            else:
                metrics[key] = engine.buffer.avg(key, window_size=window_size)

        filename = osp.join(engine.work_dir, self._filename)
        with open(filename, 'a+') as f:
            nncore.dump(metrics, f, file_format='json')
            f.write('\n')


@WRITERS.register
@nncore.bind_getter('writer')
class TensorboardWriter(Writer):

    def __init__(self, log_dir=None, graph_data_loader=None, **kwargs):
        """
        Args:
            log_dir (str, optional): directory of the tensorboard logs
            graph_data_loader (str or None, optional): name of the data_loader
                for constructing the model graph. If None, the graph will not
                be added.
                See `:meth:torch.utils.tensorboard.SummaryWriter.add_graph` for
                details about adding a graph to tensorboard.
        """
        self._log_dir = log_dir
        self._graph_data_loader = graph_data_loader
        self._kwargs = kwargs

    def open(self, engine):
        try:
            from torch.utils.tensorboard import SummaryWriter
        except ImportError:
            raise ImportError(
                "please install tensorboard to use the TensorboardWriter")

        if self._log_dir is None:
            self._log_dir = osp.join(engine.work_dir, 'tf_logs')

        self._writer = SummaryWriter(self._log_dir, **self._kwargs)

        if self._graph_data_loader is not None:
            data = next(iter(engine.data_loaders[self._graph_data_loader]))
            self._writer.add_graph(engine.model, input_to_model=data)

    def close(self, engine):
        self._writer.close()

    def write(self, engine, window_size):
        for key in engine.buffer.keys():
            if key.startswith('_'):
                continue

            if key.endswith('_'):
                tokens = key.split('_')
                log_type = tokens[-2]

                if log_type not in [
                        'histogram', 'image', 'images', 'figure', 'video',
                        'audio', 'text'
                ]:
                    raise TypeError(
                        "unsupported log type: '{}'".format(log_type))

                tag = '{}/{}'.format(''.join(tokens[:-2]), engine.mode)
                record = engine.buffer.latest(key)
                add_func = getattr(self._writer, 'add_{}'.format(log_type))
                add_func(tag, record, global_step=engine.iter)
            else:
                tag = '{}/{}'.format(key, engine.mode)
                record = engine.buffer.avg(key, window_size=window_size)

                if isinstance(record, dict):
                    self._writer.add_scalars(
                        tag, record, global_step=engine.iter)
                else:
                    self._writer.add_scalar(
                        tag, record, global_step=engine.iter)


@HOOKS.register
class EventWriterHook(Hook):

    def __init__(self, interval=50, writers=[]):
        super(EventWriterHook, self).__init__()
        self._interval = interval
        self._writers = [nncore.build_object(w, WRITERS) for w in writers]

    def _write(self, engine):
        for w in self._writers:
            w.write(engine, self._interval)

    def _empty_buffer(self, engine):
        for key in list(engine.buffer.keys()):
            if not key.startswith('_'):
                engine.buffer.clear(key)

    @master_only
    def before_launch(self, engine):
        for w in self._writers:
            w.open(engine)

    @master_only
    def after_launch(self, engine):
        for w in self._writers:
            w.close(engine)

    @master_only
    def after_train_iter(self, engine):
        if not self.last_iter_in_epoch(engine) and not self.every_n_iters(
                engine, self._interval):
            return

        self._write(engine)
        self._empty_buffer(engine)

    @master_only
    def after_val_epoch(self, engine):
        self._write(engine)
        self._empty_buffer(engine)