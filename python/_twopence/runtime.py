##################################################################
#
# Helper classes that we need to setting up a SUT's runtime.
#
# Copyright (C) 2021, 2022 Olaf Kirch <okir@suse.de>
#
##################################################################

import os
import time

from .logging import *
from .config import *

##################################################################
# This represents a loop device that can be mounted as a
# volume.
# Since the details of how a volume is used depends a lot on the
# backend, this class provides just the bare basics of allocating
# a loop device and detaching it.
#
# Note that the name and image information are persisted in
# status.conf across invocations of twopence-provision.
##################################################################
class LoopDevice(NamedConfigurable):
	info_attrs = ['name', 'image']

	schema = [
		StringAttributeSchema('image'),
		# backend specific volume ID, optional
		StringAttributeSchema('id'),
	]

	@staticmethod
	def allocateDevice():
		with os.popen("sudo losetup --find") as f:
			device = f.read().strip()

		if not device:
			return None

		return LoopDevice(device)

	def attach(self, path):
		cmd = f"sudo losetup {self.name} {path}"
		if os.system(cmd) != 0:
			error(f"Unable to set up loop device {self.name} with {path} - losetup command exited with error")
			return False

		self.image = path
		return True

	def detach(self):
		cmd = f"sudo losetup -d {self.name}"
		if os.system(cmd) != 0:
			error(f"Unable to detach loop device {self.name} - losetup command exited with error")
			return False
		return True

	def destroy(self):
		ok = self.detach()
		if self.image and os.path.exists(self.image):
			os.remove(self.image)
		return ok

##################################################################
# Helper class for wrapping information on how the twopence
# service was provisioned to the SUT.
##################################################################
class TwopenceService:
	def __init__(self, name):
		self.name = name
		self._run_dir = None
		self._status_file = None
		self.pid = None
		self.portType = None
		self.portName = None

	@property
	def run_dir(self):
		if self._run_dir is None:
			uid = os.getuid()

			dir = f"/run/user/{uid}/twopence"
			if not os.path.isdir(dir):
				os.makedirs(dir)

			self._run_dir = dir
		return self._run_dir

	@property
	def status_file(self):
		if self._status_file is None:
			self._status_file = os.path.join(self.run_dir, f"{self.name}.status")
		return self._status_file

	@property
	def log_file(self):
		return os.path.join(self.run_dir, f"{self.name}.log")

	def processStatusFile(self):
		if self._status_file is None:
			return

		with open(self._status_file) as f:
			for line in f.readlines():
				w = line.split()
				if not w:
					continue
				key, value = w
				if key == 'pid':
					self.pid = value
				elif key == 'port-type':
					self.portType = value
				elif key == 'port-name':
					self.portName = value

		assert(self.pid)
		assert(self.portType)

	def stop(self):
		if not self.pid:
			return

		info(f"Stopping twopence service running at pid {self.pid}")
		os.system(f"sudo kill -TERM {self.pid}")
		self.pid = None

