# Copyright (c) Ye Liu. All rights reserved.

import nncore
from .hooks import HOOKS, Hook
from .utils import bind_hooks


@bind_hooks
@nncore.bind_getter('hooks', 'stage', 'epoch', 'iter')
class Engine(object):

    def __init__(self, model, data_loaders, scheduler, hooks, work_dir=None):
        self._hooks = []
        self.model = model
        self.data_loaders = data_loaders
        self.scheduler = scheduler
        self.work_dir = work_dir

        self.logger = nncore.get_logger()
        self.register_hook(hooks)

        self._hooks = []
        self._stage = 0
        self._epoch = 0
        self._iter = 0

    def register_hook(self, hook, before=None):
        """
        Register a hook into the engine.

        Args:
            hook (:obj:`Hook` or dict): the hook to be registered
            before (str, optional): name of the hook to be inserted before. The
                new hook will be inserted into the end of the hook list by
                default.
        """
        if isinstance(hook, dict):
            hook = nncore.build_object(hook, HOOKS)
        elif not isinstance(hook, Hook):
            raise TypeError('hook must be a Hook or dict, but got {}'.format(
                type(hook)))

        if hook in self._hooks:
            raise ValueError("hook '{}' exists".format(hook.name))
        if before not in self._hooks:
            raise ValueError("hook '{}' not found".format(before))

        hook.on_register(self)
        if before is not None:
            idx = self._hooks.index(before)
            self._hooks.insert(idx, hook)
        else:
            self._hooks.append(hook)

    def train_step(self, *args, **kwargs):
        self.before_train_step()
        # do something
        self.after_train_step()
        self._step += 1

    def val_step(self, *args, **kwargs):
        self.before_val_step()
        # do something
        self.after_val_step()

    def train_epoch(self, *args, **kwargs):
        self.model.train()
        self.before_train_epoch()
        # do something
        self.after_train_epoch()
        self._epoch += 1

    def val_epoch(self, *args, **kwargs):
        self.model.eval()
        self.before_val_epoch()
        # do something
        self.after_val_epoch()

    def train_stage(self, *args, **kwargs):
        self.before_stage()
        # do something
        self.after_stage()
        self._stage += 1

    def launch(self, *args, **kwargs):
        self.before_launch()
        # do something
        self.after_launch()
