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
from .runtime import LoopDevice, TwopenceService

import time
import os
import shutil

##################################################################
# Generic functionality for node instances (eg VMs)
# Backends derive from this base class
##################################################################
class GenericInstance(PeristentTestInstance):
	def __init__(self, instanceConfig, workspace = None, persistentState = None):
		super().__init__(backingObject = persistentState)

		self.config = instanceConfig
		self.workspace = workspace

		self.exists = False

		self.running = False
		self.networkInterfaces = []

		self.fromNodeConfig(instanceConfig)

	@property
	def persistent(self):
		return self._backingObject

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
