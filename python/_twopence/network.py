##################################################################
#
# Network related types and classes
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

class Network:
	AF_IPv4 = 4
	AF_IPv6 = 6

	VALID_FAMILIES = (AF_IPv4, AF_IPv6)

	@staticmethod
	def familyName(af):
		if af == Network.AF_IPv4:
			return "IPv4"
		if af == Network.AF_IPv6:
			return "IPv6"
		return f"AF_{af}"

	@staticmethod
	def cantDoThisForAF(af, msg):
		name = Network.familyName(af)
		raise NotImplementedError(f"{msg} for address family {name}")

	@staticmethod
	def inet_aton(addr_string):
		import struct
		import socket

		# struct.unpack returns (ip32bit, ) so assign it first
		return struct.unpack('!I', socket.inet_aton(addr_string))[0]

	@staticmethod
	def inet_ntoa(ip):
		import struct
		import socket

		return socket.inet_ntoa(struct.pack('!I', ip))

class NetworkAddress:
	def __init__(self, family, address = None, prefix_len = None):
		assert(family in Network.VALID_FAMILIES)

		if prefix_len is None:
			if family == Network.AF_IPv4:
				prefix_len = 24
			elif family == Network.AF_IPv6:
				prefix_len = 64
			else:
				Network.cantDoThisForAF(af, "Cannot determine default prefix len")

		self.family = family
		self.address = address
		self.prefix_len = prefix_len
		self.network = f"{address}/{prefix_len}"

	def __str__(self):
		return self.network

	@staticmethod
	def parse(af, address_string):
		if af == Network.AF_IPv4:
			if '/' in address_string:
				address, prefix_len = address_string.split('/')
				return NetworkAddress(af, address, int(prefix_len))
			return NetworkAddress(af, address_string)

		Network.cantDoThisForAF(af, f"Cannot parse address string \"{address_string}\"")

	def makeHostAddrFromSubnet(self, hostNum):
		if self.family != Network.AF_IPv4:
			raise Network.cantDoThisForAF(self.family, "Cannot create addess")

		ipaddr = Network.inet_aton(self.address)

		subnetMax = (1 << self.prefix_len)
		assert(hostNum != 0 and hostNum < subnetMax - 1)

		ipaddr &= ~(subnetMax - 1)
		ipaddr |= hostNum

		new_addr = Network.inet_ntoa((ipaddr & ~(subnetMax - 1)) | hostNum)
		return NetworkAddress(Network.AF_IPv4, new_addr, self.prefix_len)

class NetworkInterface(NetworkAddress):
	pass


if __name__ == '__main__':
	addr = NetworkAddress(Network.AF_IPv4, '10.88.1.1', prefix_len = 16)
	print(f"address = {addr}")

	new_addr = addr.makeHostAddrFromSubnet(16)
	print(f"host 16 = {new_addr}")
