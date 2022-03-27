##################################################################
#
# Node abstraction for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .logging import *
from .network import *
from .persist import PeristentTestInstance
from .runtime import LoopDevice, TwopenceService, RuntimeFilesystem

import time
import os
import shutil

##################################################################
# Generic functionality for node instances (eg VMs)
# Backends derive from this base class
##################################################################
class GenericInstance(PeristentTestInstance):
	supportedVolumeTypes = {}

	def __init__(self, backend, instanceConfig, workspace = None, persistentState = None):
		super().__init__(backingObject = persistentState)

		self.backend = backend
		self.config = instanceConfig
		self.workspace = workspace

		self.exists = False

		self.running = False
		self.networkInterfaces = []

		self.runtime = None

		self.fromNodeConfig(instanceConfig)
		self._twopence = None

	@property
	def persistent(self):
		return self._backingObject

	@property
	def twopence(self):
		return self._twopence

	@property
	def containerInfo(self):
		return self.persistent.container

	def createWorkspace(self):
		path = self.workspace

		# If the instance workspace exists already, we should fail.
		# However, it may be a leftover from an aborted attempt.
		# Try to be helpful and remove the workspace IFF it is empty
		if os.path.isdir(path):
			try:	os.rmdir(path)
			except: pass

		if os.path.isdir(path):
			raise ValueError(f"workspace {path} already exists")

		os.makedirs(path)
		return path

	def workspacePath(self, name):
		return os.path.join(self.workspace, name)

	def workspaceExists(self):
		return os.path.exists(self.workspace)

	def removeWorkspace(self):
		if os.path.exists(self.workspace):
			shutil.rmtree(self.workspace)
		self.exists = False

	def createRuntime(self):
		if self.runtime is None:
			self.runtime = GenericInstanceRuntime(self.supportedVolumeTypes)
		return self.runtime

	def destroyRuntime(self):
		dropped = []
		for dev in self.loop_devices:
			debug(f"About to destroy {dev}")
			if dev.id is not None:
				self.backend.destroyVolume(dev.id)
				dev.id = None
			dev.destroy()
			dropped.append(dev)

		for dev in dropped:
			self.dropLoopDevice(dev)

	def addNetworkInterface(self, af, address, prefix_len = None):
		af = int(af)

		nif = NetworkInterface(af, address, prefix_len)
		self.networkInterfaces.append(nif)

		# Reflect the first address of this family in the status file
		self.persistNetwork()

	def clearNetworkInterfaces(self):
		self.networkInterfaces = []
		self.persistNetwork()

	def fetchNeworksFromPersistentState(self):
		self.networkInterfaces = []

		address = self.ipv4_address
		if address:
			self.addNetworkInterface(Network.AF_IPv4, address)
		address = self.ipv6_address
		if address:
			self.addNetworkInterface(Network.AF_IPv6, address)

	def persistNetwork(self):
		if self.persistent is None:
			return

		self.ipv4_address = self.getFirstNetworkAddress(Network.AF_IPv4)
		self.ipv6_address = self.getFirstNetworkAddress(Network.AF_IPv6)

	def getFirstNetworkAddress(self, af):
		for nif in self.networkInterfaces:
			if nif.family == af:
				return nif.address
		return None

	def getHostAddress(self):
		# We should get smarter than that. A lot.
		return "localhost";

	def maybeSaveKey(self, platform):
		savedPath = self.keyfile
		if savedPath is None:
			return

		info("Provisioned a new key for this instance - capturing it")
		with open(savedPath, "rb") as f:
			rawKey = f.read()
			platform.saveKey(rawKey)

	def createBuildResult(self, packageName):
		platform = self.buildResult

		if not platform:
			return None

		platform.name = packageName
		platform.build_time = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())

		keyfile = self.keyfile
		if keyfile is not None:
			with open(keyfile, "rb") as f:
				rawKey = f.read()
				platform.saveKey(rawKey)

		return platform

	def recordTarget(self, target):
		self.target = target

	@property
	def loop_devices(self):
		return self._loop_devices.values()

	def allocateLoopDevice(self):
		dev = LoopDevice.allocateDevice()
		if dev.name in self._loop_devices:
			raise ValueError("Oops, duplicate loop device name {dev.name}")
		self._loop_devices[dev.name] = dev
		return dev

	def addLoopDevice(self, dev):
		assert(isinstance(dev, LoopDevice))
		self._loop_devices[dev.name] = dev

	def dropLoopDevice(self, dev):
		if dev.name not in self._loop_devices:
			return

		assert(self._loop_devices[dev.name] is dev)
		del self._loop_devices[dev.name]

	def createEmptyBind(self, name):
		name = name.strip('/').replace('/', '_')
		return os.path.join(self.workspace, 'bind', name)

	def propagateRuntimeResources(self):
		if not self.runtime:
			return

		for volume in self.runtime.volumes:
			# If the volume description specifies an
			# ID via provide-as-resource, initialize an
			# application resource that will be saved to
			# status.conf. From there, it will be picked
			# up later by the application code.
			if volume.resource_id:
				res = self.application_resources._volumes.create(volume.resource_id)
				volume.asResource(res)

	def addVolumeResource(self, id, mountpoint):
		volumeResource = self.application_resources._volumes.create(id)
		volumeResource.mountpoint = mountpoint
		return volumeResource

	def startTwopenceInContainer(self, pid):
		twopence = TwopenceService(self.containerName)
		self._twopence = twopence

		status_file = twopence.status_file

		cmd = f"twopence test_server --port-tcp random --daemon --container-pid {pid} --status-file {status_file}"

		if True:
			cmd += f" --log-file {twopence.log_file}"
		if False:
			cmd += " --debug"

		debug(f"{self.name}: starting twopence test service: {cmd}")
		if os.system(f"sudo {cmd}") != 0:
			raise ValueError(f"Unable to start twopence test server in container {pid}")

		twopence.processStatusFile()

		if twopence.portType == 'tcp':
			addr = self.getHostAddress()
			target = f"tcp:{addr}:{twopence.portName}"
		else:
			target = f"twopence.portType:{twopence.portName}"
		info(f"{self.name}: started twopence service at pid {twopence.pid}, target is {target}")
		self.target = target

	def stopTwopence(self):
		if self._twopence is not None:
			self._twopence.stop()
			self._twopence = None

	def saveLog(self, filename, buffer):
		with self.openLog(filename) as f:
			f.write(buffer)

	def saveExecStatus(self, filename, status):
		with self.openLog(filename) as f:
			print("%s %s" % (time.ctime(), status), file = f)
			if status.output:
				print("Command output follows", file = f)
				for line in status.output:
					print(line, file = f)

	def openLog(self, filename):
		path = os.path.join(self.workspace, filename)
		return open(path, "w")

##################################################################
# Generic runtime information for SUTs
# Not all backends support all features
##################################################################
class GenericInstanceRuntime:
	def __init__(self, volumeTypes):
		self.security = None
		self.startup = None
		self._filesystem = RuntimeFilesystem(volumeTypes)
		self._sysctls = {}

	def configureVolumes(self, config):
		self._filesystem.configure(config)

	@property
	def volumes(self):
		return self._filesystem.traverse()

	def createVolume(self, type, mountpoint):
		return self._filesystem.createVolume(type, mountpoint)

	def addVolume(self, volume):
		if volume.mountpoint in self._volumes:
			raise ConfigError(f"Duplicate filesystem mount {volume.mountpoint}")
		self._volumes[volume.mountpoint] = volume

	def setSysctl(self, key, value):
		self._sysctls[key] = value

	@property
	def sysctls(self):
		return self._sysctls.items()
