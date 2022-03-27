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

##################################################################
# Helper classes for manipulating the SUTs port space, esp
# for containers.
##################################################################
class RuntimePort(NamedConfigurable):
	info_attrs = ['port', 'protocol']

	schema = [
		StringAttributeSchema('publish'),
		StringAttributeSchema('resource_id', 'provide-as-resource'),
	]

	def __init__(self, name):
		super().__init__(name)

		try:
			portNumber, proto = self.parsePort(name)
		except:
			raise ConfigError(f"Failed to parse port name \"{name}\" - should be <service>/<protocol>")

		self.port = portNumber
		self.protocol = proto

	def __str__(self):
		return f"{self.port}/{self.protocol}"

	# we propagate information on the resources we provisioned to the test case
	# via status.conf
	def asResource(self, res):
		res.protocol = self.protocol
		res.internal_port = self.port
		res.external_port = self.publish

	@staticmethod
	def parsePort(name):
		if '/' in name:
			service, proto = name.split('/')
		else:
			service, proto = name, 'tcp'

		proto = proto.lower()
		assert(proto in ('tcp', 'udp', 'sctp'))

		if service.isdigit():
			portNumber = int(service)
		else:
			import socket

			try:
				portNumber = socket.getservbyname(service, proto)
			except Exception as e:
				twopence.error(f"getservbyname does not know service {service}/{proto}")
				raise

		return portNumber, proto

class RuntimePorts(Configurable):
	schema = [
		DictNodeSchema('_ports', 'port', itemClass = RuntimePort),
	]

	def __iter__(self):
		return iter(self._ports.values())

	@staticmethod
	def _portKey(self, port, protocol):
		return f"{port}/{protocol}"

	def createPort(self, port, protocol):
		key = self._portKey(port, protocol)
		result = self._ports.get(key)
		if result is None:
			result = RuntimePort(port, protocol)
			self._ports[key] = result
		return result

##################################################################
# Helper classes for manipulating the SUTs file system, esp
# for containers.
# These classes allow you to track bind mounts, loop mounted
# devices etc.
#
# The backend specific instance class needs to provide a dictionary
# of supported volumeTypes.
##################################################################
class RuntimeFilesystem(Configurable):
	def __init__(self, volumeTypes = None, parentDirectory = None):
		self.parentDirectory = parentDirectory
		self._volumeTypes = volumeTypes
		self._volumes = {}

	@property
	def types(self):
		return self._volumeTypes

	@property
	def volumes(self):
		return self._volumes.values()

	def createVolume(self, type, mountpoint, config = None):
		volumeClass = self._volumeTypes.get(type)
		if volumeClass is None:
			raise ConfigError(f"Unable to handle volumes of type {type}")

		debug(f"Creating {type} volume at {mountpoint}")
		volume = volumeClass(mountpoint = mountpoint, volumeSet = self)
		if config:
			volume.configure(config)
		self.addVolume(volume)

		return volume

	def addVolume(self, volume):
		if volume.mountpoint in self._volumes:
			raise ConfigError(f"Duplicate filesystem mount {volume.mountpoint}")
		self._volumes[volume.mountpoint] = volume

	# do a width-first traversal
	def traverse(self):
		result = []
		for volume in self.volumes:
			result.append(volume)

		for volume in self.volumes:
			result += volume.subvolumes.traverse()

		return result

	def configure(self, config):
		for child in config:
			path = child.name
			if self.parentDirectory:
				path = os.path.join(self.parentDirectory, path)

			self.createVolume(child.type, path, child)

class RuntimeVolume(Configurable):
	info_attrs = ['mountpoint']

	schema = [
		# In theory, any volume we mount can have sub-volumes
		# In practice, a lot depends on the container engine.
		SingleNodeSchema('subvolumes', 'volumes', itemClass = RuntimeFilesystem),

		# A volume that has a provide-as-resource attribute will be
		# propagated to the Application implementation as a volume resource.
		StringAttributeSchema('resource_id', 'provide-as-resource'),
	]

	def __init__(self, mountpoint = None, volumeSet = None):
		super().__init__()

		if not mountpoint.startswith(os.path.sep):
			raise ConfigError(f"Invalid path \"{mountpoint}\" for runtime volume. Must be absolute")
		self.mountpoint = mountpoint

		self.subvolumes = RuntimeFilesystem(volumeSet.types, parentDirectory = mountpoint)

	# If any work is required to prepare the volume (eg by creating a loop device volume
	# with a file system on it), that should happen inside provision()
	def provision(self, instance):
		pass

	# we propagate information on the resources we provisioned to the test case
	# via status.conf
	def asResource(self, res):
		res.mountpoint = self.mountpoint

class RuntimeVolumeTmpfs(RuntimeVolume):
	info_attrs = RuntimeVolume.info_attrs + ['size', 'permissions']

	schema = RuntimeVolume.schema + [
		StringAttributeSchema('user'),
		StringAttributeSchema('group'),
		OctalAttributeSchema('permissions'),
		StringAttributeSchema('size'),
	]

class RuntimeVolumeBind(RuntimeVolume):
	info_attrs = RuntimeVolume.info_attrs + ['source']

	schema = RuntimeVolume.schema + [
		StringAttributeSchema('source'),
		OctalAttributeSchema('permissions'),
	]

	def asResource(self, res):
		super().asResource(res)
		res.host_path = self.source

	def provision(self, instance):
		if self.source is not None:
			return

		path = instance.createEmptyBind(self.mountpoint)
		debug(f"Provision volume {self} from {path}")
		if not os.path.isdir(path):
			os.makedirs(path, self.permissions or 0o755)

		self.source = path

class RuntimeVolumeLoop(RuntimeVolume):
	info_attrs = RuntimeVolume.info_attrs + ['size', 'permissions', 'mkfs']

	schema = RuntimeVolume.schema + [
		OctalAttributeSchema('permissions'),
		StringAttributeSchema('size'),
		StringAttributeSchema('mkfs'),
		BooleanAttributeSchema('readonly'),
	]

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

		self.loopdev = None

	def asResource(self, res):
		super().asResource(res)
		res.fstype = self.mkfs

	def provision(self, instance):
		debug(f"Provision volume {self}")

		fstype = self.mkfs or "ext4"
		imgsize = self.size or "1G"

		name = self.mountpoint.replace(os.path.sep, '-')
		path = os.path.join(instance.workspace, f"image{name}")

		self.makeEmptyImage(path, imgsize)

		loop = instance.allocateLoopDevice()
		if not loop:
			os.remove(path)
			raise ValueError(f"Cannot provision loopfs {self.mountpoint} - no loop device available")

		if not loop.attach(path):
			os.remove(path)
			raise ValueError(f"Cannot provision loopfs {self.mountpoint} - failed to attach image {path}")

		self.makeFilesystem(loop.name, fstype)
		self.loopdev = loop

	unitScales = {
		"k":	1024,
		"m":	1024 * 1024,
		"g":	1024 * 1024 * 1024,
		"kib":	1024,
		"mib":	1024 * 1024,
		"gib":	1024 * 1024 * 1024,
		"kb":	1000,
		"mb":	1000 * 1000,
		"gb":	1000 * 1000 * 1000,
	}

	def convertSize(self, imgsize):
		unit = imgsize.lstrip("0123456789.").lower()
		number = imgsize.lower().rstrip("kmgib")
		size = float(number)

		scale = self.unitScales.get(unit)
		if scale is None:
			raise ConfigError(f"bad unit in image size {imgsize} for loop device {self.mountpoint}")

		return size * scale

	def makeEmptyImage(self, path, imgsize):
		blockSizes = ["1M", "64K", "1K"]

		size = self.convertSize(imgsize)
		for bs in blockSizes:
			bsValue = self.convertSize(bs)
			if size >= 16 * bsValue:
				break

		size = int(size / bsValue + 0.5)

		debug(f"Creating image in {path}, {size} blocks of {bs} each")
		cmd = f"dd if=/dev/zero of={path} bs={bs} count={size}"
		if os.system(cmd) != 0:
			error(f"Unable to create loop image at {path} - dd command exited with error")
			if os.path.exists(path):
				os.remove(path)
			raise ValueError("Failed to create image")

	def setupLoopDevice(self, path):
		with os.popen("sudo losetup --find") as f:
			device = f.read().strip()

		if not device:
			return None

		cmd = f"sudo losetup {device} {path}"
		if os.system(cmd) != 0:
			error(f"Unable to set up loop device {device} with {path} - losetup command exited with error")
			return None

	def makeFilesystem(self, device, fstype):
		info(f"Creating {fstype} file system at {device}")
		cmd = f"sudo mkfs -t {fstype} {device}"
		if os.system(cmd) != 0:
			error(f"Unable to format loop image at {device} - mkfs command exited with error")
			raise ValueError("Failed to create image")
