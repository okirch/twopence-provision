##################################################################
#
# podman backend for twopence provisioner
#
# Copyright (C) 2022 Olaf Kirch <okir@suse.de>
#
##################################################################
import os
import json
import shutil
import copy
import time

from .logging import *
from .backend import Backend
from .runner import Runner
from .instance import *
from .provision import *
from .network import *
from .config import Config, Configurable, ConfigError, Schema
from .util import DottedNumericVersion

from .oci import ImageFormatDockerRegistry, ImageReference, ImageConfig, ContainerStatus

class PodmanImageInfo:
	def __init__(self, registry, names, version = None):
		self._registry = registry
		self.names = names
		if version:
			self._version = DottedNumericVersion(version)
		else:
			self._version = 'latest'
#		self.origin = origin or self.ORIGIN_LOCAL

	@property
	def version(self):
		return str(self._version)

	@property
	def registry(self):
		return self._registry or "local"

	@property
	def islocal(self):
		return self._registry is None

	@version.setter
	def version(self, value):
		self._version = DottedNumericVersion(value)

	def __str__(self):
		if self.names:
			name = self.names[0]
		else:
			name = "unknown"
		return f"image {name}:{self.version}, origin {self.registry}"

	# practically the same, except for the version
	def similar(self, other):
		if not isinstance(other, self.__class__):
			return False

		if not set(self.names).intersection(set(other.names)):
			return False

		if self.registry == other.registry:
			return True

		# A local and a remote image always match
#		if self.islocal or other.islocal:
#			return True

		return False

	def __eq__(self, other):
		if not self.similar(other):
			return False
		return self._version == other._version

	def __lt__(self, other):
		if not self.similar(other):
			return False
		return self._version < other._version

	def __le__(self, other):
		if not self.similar(other):
			return False
		return self._version <= other._version

	@staticmethod
	def queryRegistry(searchKey):
		fmt = ImageFormatDockerRegistry(searchKey)
		image = fmt.query()

		if image is None:
			raise ValueError(f"Unable to find image {searchKey}")

		config = image.getConfig()
		if config is None:
			raise ValueError(f"Failed to load config for {searchKey}")

		return config
		# return PodmanImageInfo(searchKey.registry, [image.name], config.imageVersion)

class PodmanBoxMeta:
	pass

class ContainerRuntimeNetwork:
	def __init__(self, id):
		self.id = id

class PodmanNetwork(ContainerRuntimeNetwork):
	def __init__(self, id, ipv4_subnet = None, ipv4_gateway = None):
		super().__init__(id)

		self.ipv4_subnet = ipv4_subnet
		self.ipv4_gateway = ipv4_gateway

		self._next_host_index = 16

	def __str__(self):
		args = [self.id]
		if self.ipv4_subnet:
			args.append(f"subnet={self.ipv4_subnet}")
		if self.ipv4_gateway:
			args.append(f"gateway={self.ipv4_gateway}")
		args = ", ".join(args)

		return f"Network({args})"

	@property
	def ipv4_subnet(self):
		return self._ipv4_subnet

	@ipv4_subnet.setter
	def ipv4_subnet(self, value):
		if value is not None:
			value = NetworkAddress.parse(Network.AF_IPv4, value)
		self._ipv4_subnet = value

	@property
	def ipv4_gateway(self):
		return self._ipv4_gateway

	@ipv4_gateway.setter
	def ipv4_gateway(self, value):
		if value is not None:
			value = NetworkAddress.parse(Network.AF_IPv4, value)
		self._ipv4_gateway = value

	def claimNextIPv4Address(self):
		address = self.claimIPv4Address(self._next_host_index)
		if address is not None:
			self._next_host_index += 1

		# FIXME: avoid claiming the gateway!

		return address

	def claimIPv4Address(self, hostIndex):
		subnet = self.ipv4_subnet

		if subnet is None:
			return None

		if subnet.prefix_len < 20:
			hostIndex += 4 * 256

		return subnet.makeHostAddrFromSubnet(hostIndex)

class PodmanImageListing:
	def __init__(self):
		self.images = []

	def create(self, *args, **kwargs):
		image = ImageConfig(*args, **kwargs)
		self.images.append(image)
		return image

	def find(self, searchKey):
		debug(f"ImageListing.find({searchKey})")
		for image in self.images:
			for name in image.imageNames:
				if name == searchKey:
					return image
		return None

	def __contains__(self, wanted):
		if wanted is None:
			return False
		for image in self.images:
			if image == wanted:
				return True
		return False


class ContainerCommand:
	def __init__(self, datadir, command, *argv):
		self.datadir = datadir
		self.command = command
		self.argv = argv

	def getInvocation(self, mountPoint):
		if self.datadir:
			path = os.path.join(mountPoint, self.command)
		else:
			path = self.command
		return " ".join([path] + list(self.argv))

class PodmanInstance(GenericInstance):
	def __init__(self, instanceConfig, instanceWorkspace, persistentState, command = None, containerName = None, **kwargs):
		super().__init__(instanceConfig, instanceWorkspace, persistentState)
		self.container = None
		self.containerId = None
		self.containerName = containerName
		self.command = command
		self.raw_state = None

		self.autoremove = False

	def setContainerState(self, status):
		self.container = status

		if status is None:
			self.containerId = None
			self.raw_state = 'dead'
		else:
			self.containerId = status.id
			self.raw_state = status.state
		self.running = self.raw_state == 'running'

	@property
	def imageSearchKey(self):
		return self.config.podman.key

	@property
	def image(self):
		imageString = None
		if self.persistent:
			imageString = self.persistent.image
		if imageString:
			return ImageReference.parse(imageString)
		return None

	@image.setter
	def image(self, imageKey):
		debug(f"{self.name}: going to use {imageKey}")
		if self.persistent:
			self.persistent.image = str(imageKey)

	@property
	def short_id(self):
		if not self.containerId:
			return None

		return self.containerId[:12]

class PodmanNodeConfig(Configurable):
	info_attrs = ['registry', 'image', 'timeout']

	schema = [
		Schema.StringAttribute('image'),
		Schema.StringAttribute('registry'),
		Schema.FloatAttribute('timeout', default_value = 120),
	]

	def __init__(self):
		super().__init__()
		# currently, we do not support specifying a concrete version
		# in the config file. We just record this information when
		# deciding on the exact image to use, so that it can be
		# saved if needed
		self.version = None

	@property
	def key(self):
		return ImageReference(self.registry, self.image)

class PodmanBackend(Backend):
	name = "podman"

	schema = [
		Schema.StringAttribute('template'),
		Schema.FloatAttribute('timeout', default_value = 120),
	]

	def __init__(self):
		debug("Created podman backend")
		super().__init__()

		self.runner = Runner()
		self.provisioner = Provisioner()

		# the podman image listing
		self.listing = None

		# the podman networks available
		self.networks = None
		# the podman network to use
		self._network = None

	def attachNode(self, node):
		# detect whether the node we want to provision/build has twopence enabled. If
		# it does, we also enable "twopence-tcp", which essentially configures
		# the twopence test server to listen on a TCP port
#		if 'twopence' in node.features + node.requestedBuildOptions:
#			node.requestedBuildOptions.append('twopence-tcp')

		try:
			return node.podman
		except:
			pass

		node.podman = PodmanNodeConfig()
		return node.podman

	def createInstance(self, instanceConfig, instanceWorkspace, persistentState):
		assert(self.testcase)
		containerName = f"twopence-{self.testcase}-{instanceConfig.name}"

		return PodmanInstance(instanceConfig, instanceWorkspace, persistentState,
					containerName = containerName)

	def detect(self, topology, instances):
		found = []
		for instance in instances:
			if self.detectInstance(instance):
				found.append(instance)
		return found

	def detectInstance(self, instance):
		debug(f"detectInstance({instance.name})")
		return self.detectInstanceState(instance)
		debug(f"Detected instance {instance.name} (state {instance.raw_state})")

	def findContainer(self, name):
		if name is None:
			return None

		print(f"Checking for running container {name}")
		with os.popen("sudo podman ps -a --format json") as f:
			data = json.load(f)

		for entry in data:
			status = ContainerStatus(entry)
			if name in status.names:
				print(f"  found {status}")
				return status
		return None

	def identifyImage(self, searchKey):
		# See if we have the requested name and tag
		have = self.findImage(searchKey)
		if have is None:
			return None

		# Our tag may be out of date with upstream, so make sure we
		# agree on the version
		if not searchKey.registry or searchKey.registry == 'localhost':
			debug(f"Using local image {have}")
			return have

		available = PodmanImageInfo.queryRegistry(searchKey)
		if not available:
			warn(f"{searchKey}: registry does not know about this image")
			info(f"Using local image {have}")
			return have

		if available.imageVersion == have.imageVersion:
			debug(f"Latest version for {searchKey} is {have.imageVersion}; already present")
			return have

		debug(f"{searchKey}: our version is outdated (ours={have.imageVersion}, available={available.imageVersion}")
		return None

	def downloadImage(self, instance):
		searchKey = instance.imageSearchKey
		imageInfo = self.identifyImage(searchKey)
		if imageInfo is None:
			self.addImage(searchKey)
			imageInfo = self.identifyImage(searchKey)

		if imageInfo is None:
			raise ValueError(f"Tried to pull {searchKey}, but still can't find the image locally")

		key = copy.copy(searchKey)
		key.tag = imageInfo.imageVersion
		instance.image = key

		return True

	def prepareInstance(self, instance):
		for stage in instance.config.cookedStages():
			if stage.reboot:
				raise ConfigError(f"Cannot provision node {instanceConfig.name} - stage {stage} would require a reboot")

		# instance.config gives us a list of scriptlets for provisioning.
		# We turn these into a set of config.vm.provision blocks for the
		# Podmanfile
		provisioning = self.buildProvisioning(instance.config)

		# HACK ATTACK - shortcut for testing
		if False:
			provisioning = []

		provisioning.append(f'''
echo "Lift off to the tune of the PodmanDancingMonkey"
exec /mnt/sidecar/twopence-test-server --port-tcp 4000 >/dev/null 2>/dev/null
''')

		path = instance.workspacePath("provision.sh")
		with open(path, "w") as f:
			print("#!/bin/bash", file = f)
			for block in provisioning:
				print(block, file = f)

		os.chmod(path, 0o755)
		# os.system(f"cat {path}")

		command = ContainerCommand(instance.workspace, "provision.sh")
		self.createSidecar(command.datadir)

		instance.command = command
		return instance

	def createInstanceWorkspace(self, workspace, instanceConfig):
		assert(workspace)

		workspace = os.path.join(workspace, instanceConfig.name)

		# If the instance workspace exists already, we should fail.
		# However, it may be a leftover from an aborted attempt.
		# Try to be helpful and remove the workspace IFF it is empty
		if os.path.isdir(workspace):
			try:	os.rmdir(workspace)
			except: pass

		if os.path.isdir(workspace):
			raise ValueError("workspace %s already exists" % workspace)

		os.makedirs(workspace)
		return workspace

	def buildProvisioning(self, instanceConfig):
		result = []
		for s in instanceConfig.cookedStages():
			formatted = ""

			if s.reboot:
				raise ValueError("containers can't be rebooted")

			formatted += s.format()
			result.append(formatted)

		return result

	# This is not really a sidecar; I just called it so.
	# What we do here is copy twopence-test-server and all the shared libs
	# it requries into a directory, which is then mounted into the container
	# at runtime.
	def createSidecar(self, datadir):
		path = os.path.join(datadir, "sidecar")
		if os.system(f"twopence create-sidecar {path}") != 0:
			raise ValueError("Failed to create container sidecar for the test server")

		return path

	def buildMachineConfig(self, instanceConfig):
		def format_string_attr(ruby_var_name, attr_name):
			object = instanceConfig
			for n in attr_name.split('.'):
				object = getattr(object, n, None)
				if object is None:
					return

			assert(type(object) == str)
			result.append("%s = '%s'" % (ruby_var_name, object))

		result = []
		format_string_attr("config.vm.box", "podman.image")
		format_string_attr("config.vm.hostname", "name")
		format_string_attr("config.ssh.private_key_path", "keyfile")
		format_string_attr("twopence_platform", "platform.name")
		format_string_attr("twopence_vendor", "platform.vendor")
		format_string_attr("twopence_os", "platform.os")
		format_string_attr("twopence_arch", "platform.arch")

		return result

	def startInstance(self, instance):
		if instance.running:
			print("Cannot start instance %s - already running" % instance.name)
			return False

		when = time.ctime()
		timeout = instance.config.podman.timeout or 120

		print("Starting %s instance (timeout = %d)" % (instance.name, timeout))
		argv = ["sudo", "podman", "run"]

		if instance.autoremove:
			argv.append("--rm=true")

		net = self.network
		if net is not None:
			argv += ["--network", net.id]
			addr = net.claimNextIPv4Address()
			if addr:
				verbose(f"Assigning network address {addr}")
				instance.addNetworkInterface(Network.AF_IPv4, addr.address, addr.prefix_len)
				argv += ["--ip", addr.address]

		# The default we make the test server listen on
		argv += ["--expose", "4000"]
		argv += ["--publish", "4000"]

		argv += ["--name", instance.containerName]

		if instance.command:
			command = instance.command
			argv += ["--mount", f"type=bind,src={command.datadir},target=/mnt"]
			exec = command.getInvocation("/mnt")
		else:
			exec = "pause"
		argv += [str(instance.config.podman.key)]
		argv.append(exec)

		cmd = " ".join(argv)
		status = self.runShellCmd(cmd, timeout = timeout, abandonOnString = "PodmanDancingMonkey")

		verbose("Saving output to podman_run.log")
		instance.saveExecStatus("podman_run.log", status)

		if status.exit_code != None:
			print(f"Cannot start instance {instance.name} - podman up failed ({status})")
			return False

		instance.recordStartTime(when)
		return True

	def updateInstanceTarget(self, instance):
		target = None

		if instance.exists:
			addr = instance.getFirstNetworkAddress(Network.AF_IPv4)
			if addr:
				# hard-coded for now
				target = "tcp:%s:4000" % addr

		instance.recordTarget(target)

	def detectInstanceState(self, instance):
		running = self.findContainer(instance.containerName)
		instance.setContainerState(running)

		if instance.running:
			debug(f"Instance {instance.name} is {running}")
		if instance.workspaceExists():
			instance.exists = True

		return instance.running or instance.exists

	def detectInstanceNetwork(self, instance):
		return False

	def stopInstance(self, instance, force = False, destroy = False):
		if destroy:
			return self.destroyInstance(instance)

		if not instance.running and not force:
			debug(f"not stopping instance {instance.name} - already stopped")
			return

		verbose(f"Stopping {instance.name} instance")
		cmd = f"sudo podman stop {instance.container.id}"

		status = self.runShellCmd(cmd, cwd = instance.workspace, timeout = 30)
		if not status:
			raise ValueError(f"{instance.name}: podman stop failed: {status}")

		self.detectInstanceState(instance)
		if instance.running:
			error(f"{instance.name}: podman failed to stop container")
			return False

		instance.recordStartTime(None)
		return True

	def destroyInstance(self, instance):
		verbose(f"Destroying {instance.name} instance")
		if instance.running:
			status = self.runPodman("kill", instance)
			if not status:
				raise ValueError(f"{instance.name}: podman kill failed: {status}")

		running = self.findContainer(instance.containerName)
		debug(f"{instance.containerId} status {running}")
		if running is not None:
			status = self.runPodman("rm", instance, timeout = 30)
			if not status:
				raise ValueError(f"{instance.name}: podman rm failed: {status}")

		instance.removeWorkspace()
		instance.dead = True

		return True

	def saveInstanceImage(self, instance, platform):
		if instance.containerId is None:
			raise ValueError(f"Cannot save instance {instance.name} - no container id")

		if instance.image is None:
			raise ValueError(f"Cannot save instance {instance.name} - don't know original image name")

		outputName = f"{platform.name}:{instance.image.tag}"
		outputImage = ImageReference("localhost", outputName)

		existingImage = self.findImage(outputImage)

		# FIXME: we should probably remove most if not all of the labels
		# and replace them with something more a propos

		cmd = f"sudo podman commit {instance.containerId} {outputImage}"
		status = self.runShellCmd(cmd, cwd = instance.workspace, timeout = 120)
		if not status:
			raise ValueError(f"{instance.name}: podman package failed {status}")

		info("Created image {outputImage}")

		if existingImage:
			self.runShellCmd(f"sudo podman rmi {existingImage.id}")

		# Inside the platform {} decl, create backend specific info:
		#	backend podman {
		#		image "leap-15.4-container-fips-twopence:version";
		#	}
		platform.addBackend(self.name, image = outputName)

		# Clear any cached image listing
		self.listing = None

	def packageInstance(self, instance, packageName):
		assert(instance.config.buildResult)
		platform = instance.config.buildResult
		platform.name = packageName
		platform.build_time = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())

		self.saveInstanceImage(instance, platform)

		platform.finalize()
		platform.save()
		return True

	##################################################################
	# List the available boxes using podman images
	##################################################################
	def listImages(self):
		if self.listing:
			return self.listing

		data = None
		with os.popen("sudo podman images --format json") as f:
			data = json.load(f)

		if not data:
			# We could fall back to using virsh directly...
			raise ValueError("podman: could not list images")

		listing = PodmanImageListing()
		for entry in data:
			config = listing.create(entry)

		self.listing = listing
		return listing

	def findImage(self, searchKey):
		debug(f"Looking for {searchKey} in local image store")
		return self.listImages().find(searchKey)

	@property
	def network(self):
		if self._network is None:
			self._network = self.findNetwork('podman')
		return self._network

	def findNetwork(self, name):
		if self.networks is None:
			self.networks = self.listNetworks()
		for net in self.networks:
			if net.id == name:
				return net
		return None

	def listNetworks(self):
		networks = []

		data = None
		cmd = "sudo podman network ls --format json"
		with os.popen(cmd) as f:
			data = json.load(f)

		for entry in data:
			id = entry.get('Name')
			if id is not None:
				net = PodmanNetwork(id)
				networks.append(net)

		for net in networks:
			cmd = f"sudo podman network inspect {net.id}"
			with os.popen(cmd) as f:
				data = json.load(f)

				for entry in data:
					for plugin in entry.get('plugins', []):
						if plugin.get('type') != 'bridge':
							continue

						ipam = plugin.get('ipam')
						if not ipam:
							continue
						for r in ipam.get('ranges', []):
							for something in r:
								subnet = something.get('subnet')
								gateway = something.get('gateway')
								net.ipv4_subnet = subnet
								net.ipv4_gateway = gateway

			print(net)
		return networks

	##################################################################
	# Add a box from the given image
	##################################################################
	def addImage(self, image):
		verbose(f"Adding container image {image}")

		cmd = f"sudo podman pull {image}"
		if not self.runShellCmd(cmd, timeout = 60):
			raise ValueError("Failed to pull image {image}")

		# Clear any cached listing
		self.listing = None

	##################################################################
	# Run a podman command inside an instance workspace
	##################################################################
	def runPodman(self, subcommand, instance, retries = 3, **kwargs):
		id = instance.containerId
		if not id:
			return None

		command = f"sudo podman {subcommand} {id}"
		return self.runShellCmd(command, **kwargs)

	def runShellCmd(self, *args, **kwargs):
		return self.runner.run(*args, **kwargs)

##################################################################
# Initialize the schema for any classes that use
# Configurable's schema approach
##################################################################
Schema.initializeAll(globals())

