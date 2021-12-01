##################################################################
#
# Node abstraction for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import time
import os

class Network:
	AF_IPv4 = 4
	AF_IPv6 = 6

	VALID_FAMILIES = (AF_IPv4, AF_IPv6)

class NetworkInterface:
	def __init__(self, family, address = None, prefix_len = None):
		assert(family in Network.VALID_FAMILIES)

		if prefix_len is None:
			if family == Network.AF_IPv4:
				prefix_len = 24
			elif family == Network.AF_IPv6:
				prefix_len = 64
			else:
				raise ValueError("not default prefix len for AF %s" % af)

		self.family = family
		self.address = address
		self.prefix_len = prefix_len
		self.network = "%s/%s" % (address, prefix_len)

	def __str__(self):
		return "%s/%s" % (self.address, self.prefix_len)

##################################################################
# Generic functionality for node instances (eg VMs)
# Backends derive from this base class
##################################################################
class GenericInstance:
	def __init__(self, instanceConfig, workspace, persistentState = None):
		self.config = instanceConfig
		self.workspace = workspace
		self.persistent = persistentState
		self.name = instanceConfig.name

		self.exists = False

		self.running = False
		self.networkInterfaces = []

		if self.persistent:
			instanceConfig.persistInfo(self.persistent)

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
		if self.persistent is None:
			return

		self.persistent.set_value("start-time", when)

	def recordKeyfile(self, path):
		if self.persistent is None:
			return

		self.persistent.set_value("keyfile", path)

	def recordTarget(self, target):
		if self.persistent is None:
			return

		self.persistent.set_value("target", target)

	def saveLog(self, filename, buffer):
		with self.openLog(filename) as f:
			f.write(buffer)

	def saveExecStatus(self, filename, status):
		with self.openLog(filename) as f:
			print("%s %s" % (time.ctime(), status), file = f)
			if status.output:
				print("Command output follows", file = f)
				f.write(status.output)

	def openLog(self, filename):
		path = os.path.join(self.workspace, filename)
		return open(path, "w")
