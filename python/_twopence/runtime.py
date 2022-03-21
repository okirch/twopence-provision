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

