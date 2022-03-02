##################################################################
#
# Node abstraction for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .logging import *
from .network import *
import time
import os
import shutil

##################################################################
# Generic functionality for node instances (eg VMs)
# Backends derive from this base class
##################################################################
class GenericInstance:
	def __init__(self, instanceConfig, workspace = None, persistentState = None):
		self.config = instanceConfig
		self.workspace = workspace
		self.persistent = persistentState
		self.name = instanceConfig.name

		self.exists = False

		self.running = False
		self.networkInterfaces = []

		if self.persistent:
			instanceConfig.persistInfo(self.persistent)

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
		if self.persistent is None:
			return

		self.networkInterfaces = []

		address = self.persistent.ipv4_address
		if address:
			self.addNetworkInterface(Network.AF_IPv4, address)
		address = self.persistent.ipv6_address
		if address:
			self.addNetworkInterface(Network.AF_IPv6, address)

	def persistNetwork(self):
		if self.persistent is None:
			return

		self.persistent.ipv4_address = self.getFirstNetworkAddress(Network.AF_IPv4)
		self.persistent.ipv6_address = self.getFirstNetworkAddress(Network.AF_IPv6)

	def getFirstNetworkAddress(self, af):
		for nif in self.networkInterfaces:
			if nif.family == af:
				return nif.address
		return None

	def recordStartTime(self, when):
		if self.persistent:
			self.persistent.set_value("start-time", when)

	def recordKeyfile(self, path):
		if self.persistent:
			self.persistent.keyfile = path

	def _getPersistent(self, name):
		if self.persistent:
			return self.persistent.get_value(name)

	def _setPersistent(self, name, value):
		if self.persistent:
			self.persistent.set_value(name, value)

	@property
	def keyfile(self):
		if self.persistent:
			return self.persistent.keyfile

	@keyfile.setter
	def keyfile(self, value):
		if self.persistent:
			self.persistent.keyfile = value

	def maybeSaveKey(self, platform):
		savedPath = self.keyfile
		if savedPath is None:
			return

		info("Provisioned a new key for this instance - capturing it")
		with open(savedPath, "rb") as f:
			rawKey = f.read()
			platform.saveKey(rawKey)

	def createBuildResult(self, packageName):
		if not self.config or not self.config.buildResult:
			return None

		platform = self.config.buildResult
		platform.name = packageName
		platform.build_time = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())

		keyfile = self.keyfile
		if keyfile is not None:
			with open(keyfile, "rb") as f:
				rawKey = f.read()
				platform.saveKey(rawKey)

		return platform

	def recordTarget(self, target):
		if self.persistent:
			self.persistent.set_value("target", target)

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
