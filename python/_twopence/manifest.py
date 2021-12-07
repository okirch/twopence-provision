##################################################################
#
# When provisioning instances for a test run, the user
# first calls "twopence provision init", which creates a
# Bill of Materials file in the workstapce directory.
# Inside this BOM file, it records the backend, the config
# files, and some other path names.
#
# This makes subsequent invocations of the provisioner simpler,
# as those only need a pointer to the workspace and can pick
# up all pertinent information from the BOM file.
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import curly
import os

from .logging import *

class BOM:
	def __init__(self, workspace):
		self.workspace = workspace
		self.backend = None
		self.status = None
		self.config = []

		self._saveConfigs = False
		self._path = os.path.join(workspace, "bom.conf")

	@property
	def path(self):
		return self._path

	@property
	def exists(self):
		return os.path.exists(self._path)

	def load(self):
		self.data = curly.Config(self._path)
		debug("Loaded BOM from %s" % self.path)

		tree = self.data.tree()

		self.backend = tree.get_value("backend")
		self.status = tree.get_value("status")
		self.logspace = tree.get_value("logspace")
		self.config = tree.get_values("config")

		return True

	def addConfig(self, path):
		if self._saveConfigs:
			path = self.saveConfig(path)
		else:
			path = os.path.realpath(path)
		self.config.append(path)

	def saveConfig(self, path):
		import shutil

		if not os.path.isdir(self.workspace):
			os.makedirs(self.workspace)

		copied = os.path.join(self.workspace, os.path.basename(path))
		shutil.copyfile(path, copied)

		return copied

	def save(self):
		bom = curly.Config()
		tree = bom.tree()

		if not self.status:
			self.status = os.path.join(self.workspace, "status.conf")
		if not self.logspace:
			self.logspace = os.path.join(self.workspace, "run")

		tree.set_value("backend", self.backend)
		tree.set_value("status", self.status)
		tree.set_value("logspace", self.logspace)
		tree.set_value("config", self.config)

		if not os.path.isdir(self.workspace):
			os.makedirs(self.workspace)

		bom.save(self._path)

	def remove(self):
		if self.exists:
			os.remove(self._path)
