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
from .runtime import *
from .provision import *
from .network import *
from .container import *
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

class PodmanVolumeTmpfs(RuntimeVolumeTmpfs):
	def addCommandOptions(self, argv):
		size = self.size or "512M"

		options = ["type=tmpfs", f"tmpfs-size={size}"]
		if self.permissions:
			# Is there a way to display integers in C style octal syntax, but using f strings?
			options.append("tmpfs-mode=0%o" % self.permissions)
		options.append(f"destination={self.mountpoint}")

		argv.append("--mount=" + ",".join(options))

class PodmanVolumeBind(RuntimeVolumeBind):
	def addCommandOptions(self, argv):
		if self.source is None:
			raise ValueError("Cannot bind mount {self.mountpoint}: no source directory specified")

		options = ["type=bind", f"src={self.source}", f"target={self.mountpoint}"]
		argv.append("--mount=" + ",".join(options))

class PodmanVolumeLoop(RuntimeVolumeLoop):
	def provision(self, instance):
		# First, create the loop device and put a file system on it
		super().provision(instance)

		loopdev = self.loopdev

		cmd = f"sudo podman volume create --opt device={loopdev.name} --opt type={self.mkfs}"
		with os.popen(cmd) as f:
			loopdev.id = f.read().strip()

		if loopdev.id is None:
			raise ValueError("Unable to provision podman volume for device={loopdev.name}")

	def addCommandOptions(self, argv):
		dev = self.loopdev
		if dev is None or not dev.id:
			raise ValueError("Cannot mount volume {self.mountpoint}: loop device not set up")

		options = ["type=volume", f"source={dev.id}"]
		options.append(f"destination={self.mountpoint}")
		if self.readonly:
			options.append("ro=true")

		argv.append("--mount=" + ",".join(options))

class ContainerImageListing:
	def __init__(self):
		self.images = []

	def addEntryJSON(self, *args, **kwargs):
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


class PodmanInstance(GenericInstance):
	supportedVolumeTypes = {
		'tmpfs':	PodmanVolumeTmpfs,
		'bind':		PodmanVolumeBind,
		'loopfs':	PodmanVolumeLoop,
	}

	def __init__(self, backend, instanceConfig, instanceWorkspace, persistentState, command = None, containerName = None, **kwargs):
		super().__init__(backend, instanceConfig, instanceWorkspace, persistentState)
		self.container = None
		self.containerId = None
		self.containerName = containerName
		self.command = command
		self.raw_state = None

		self.autoremove = False
		self.target = None

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

class PodmanBackend(Backend):
	name = "podman"

	twopenceBuildOptions = []
	twopenceRepositories = []

	schema = [
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
		try:
			return node.podman
		except:
			pass

		node.podman = ContainerNodeConfig()
		return node.podman

	def createInstance(self, instanceConfig, instanceWorkspace, persistentState):
		assert(self.testcase)
		containerName = f"twopence-{self.testcase}-{instanceConfig.name}"

		return PodmanInstance(self, instanceConfig, instanceWorkspace, persistentState,
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

		debug(f"Checking for running container {name}")
		with os.popen("sudo podman ps -a --format json") as f:
			data = json.load(f)

		for entry in data:
			status = ContainerStatus(entry)
			if name in status.names:
				debug(f"  found {status}")
				return status
		return None

	def findContainerPID(self, id):
		if id is None:
			return None

		debug(f"Checking PID of running container {id}")
		with os.popen(f"sudo podman container inspect {id}") as f:
			data = json.load(f)

		if len(data) > 1:
			error(f"ambiguous data in output of podman container inspect {id}")
		elif data:
			entry = data[0]
			state = entry.get('State')
			if state:
				pid = state.get('Pid')
				if pid:
					debug(f"  found {pid}")
					return pid
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
		instance.image = str(key)

		return True

	def prepareInstance(self, instance):
		# applications are handled by prepareApplication
		assert(not instance.config.platform.isApplication)

		for stage in instance.config.cookedStages():
			if stage.reboot:
				raise ConfigError(f"Cannot provision node {instance.config.name} - stage {stage} would require a reboot")

		runtime = self.prepareRuntime(instance)

		# instance.config gives us a list of scriptlets for provisioning.
		# We turn these into a script that we execute in the container
		provisioning = self.buildProvisioning(instance.config)

		# HACK ATTACK - shortcut for testing
		if False:
			provisioning = []

		provisioning.append(f'''
echo "Lift off to the tune of the PodmanDancingMonkey"
exec sleep infinity
''')

		path = instance.workspacePath("provision.sh")
		with open(path, "w") as f:
			print("#!/bin/bash", file = f)
			for block in provisioning:
				print(block, file = f)

		os.chmod(path, 0o755)
		# os.system(f"cat {path}")

		bindMount = runtime.createVolume("bind", "/mnt")
		bindMount.source = instance.workspace

		runtime.startup.command = "/mnt/provision.sh"
		runtime.startup.success = "PodmanDancingMonkey"

		return instance

	def prepareApplication(self, instance):
		if instance.config.cookedStages():
			warning("It appears that {instance.config.name} specifies one or more build stages.")
			warning("Application images cannot be modified by twopence")

		self.prepareRuntime(instance)

		return instance

	def prepareRuntime(self, instance):
		runtime = instance.createRuntime()
		runtime.startup = ContainerStartupConfig()

		runtimeConfig = instance.config.podman.runtime
		if runtimeConfig is None:
			return runtime

		if runtimeConfig.volumes:
			runtime.configureVolumes(runtimeConfig.volumes)

			for volume in runtime.volumes:
				volume.provision(instance)

		if runtimeConfig.ports:
			runtime.configurePorts(runtimeConfig.ports)

		runtime.security = runtimeConfig.security

		if runtimeConfig.startup:
			runtime.startup = runtimeConfig.startup
		debug(f"ARGUMENTS {runtime.startup.arguments}")

		sysctl = runtimeConfig.sysctl
		if sysctl:
			for key, value in sysctl.items():
				if type(value) == list:
					assert(len(value) == 1)
					value = value[0]
				runtime.setSysctl(key, value)

		return runtime

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

	def startInstance(self, instance):
		if instance.running:
			error(f"Cannot start instance {instance.name} - already running")
			return False

		runtime = instance.runtime
		debug(f"runtime is {runtime}")

		when = time.ctime()
		timeout = instance.config.podman.timeout or 120

		info(f"Starting {instance.name} instance (timeout = {timeout})")
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
		if False:
			argv += ["--expose", "4000"]
			argv += ["--publish", "4000"]

		if runtime.security:
			sec = runtime.security
			if sec.privileged:
				argv += ["--privileged"]

		for volume in runtime.volumes:
			volume.addCommandOptions(argv)

		for port in runtime.ports:
			if port.publish:
				argv += ["--publish", str(port.publish) ]

		for key, value in runtime.sysctls:
			argv += [f"--sysctl {key}='{value}'"]

		argv += ["--name", instance.containerName]

		# The container image ID
		argv += [str(instance.config.podman.key)]

		startup = runtime.startup
		if startup.command:
			argv.append(startup.command)
		else:
			# FIXME: we might want to make sure that the image defines
			# an entrypoint
			pass

		if startup.arguments:
			argv += startup.arguments

		cmd = " ".join(argv)

		print(f"Command is {cmd}")

		if startup.success:
			status = self.runShellCmd(cmd, timeout = timeout, abandonOnString = startup.success)
		else:
			raise ConfigError("Please define a startup.success string for this application")

		verbose("Saving output to podman_run.log")
		instance.saveExecStatus("podman_run.log", status)

		if status.exit_code != None:
			print(f"Cannot start instance {instance.name} - podman up failed ({status})")
			return False

		instance.start_time = when

		# persist relevant info on the container
		containerInfo = instance.containerInfo

		containerInfo.name = instance.containerName
		containerInfo.pid = self.findContainerPID(instance.containerName)
		if containerInfo.pid is None:
			raise ConfigError(f"Unable to locate container {instance.containerName}")
		# FIXME: we could also store the ID of the container process in containerInfo.id

		if instance.twopence is None:
			twopence = self.startTwopenceInContainer(containerInfo, instance.getHostAddress())
			instance.twopence = twopence
			instance.target = twopence.target

			info(f"{instance.name}: started twopence service at pid {twopence.pid}, target is {twopence.target}")

		return True

	def startTwopenceInContainer(self, containerInfo, hostAddress):
		twopence = TwopenceService(containerInfo.name)
		twopence.startInContainer(containerInfo.pid, hostAddress)
		return twopence

		self.target = self.twopence.startInContainer(pid, self.getHostAddress())

	def updateInstanceTarget(self, instance):
		if instance.exists:
			if instance.target is not None:
				return

			addr = instance.getFirstNetworkAddress(Network.AF_IPv4)
			if addr:
				# hard-coded for now
				instance.target = "tcp:%s:4000" % addr
				return

		instance.target = None

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

		instance.start_time = None
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

	def destroyVolume(self, volumeID):
		if not volumeID:
			return

		self.runShellCmd(f"sudo podman volume rm {volumeID}")

	def saveInstanceImage(self, instance, platform):
		if instance.containerId is None:
			raise ValueError(f"Cannot save instance {instance.name} - no container id")

		if instance.image is None:
			raise ValueError(f"Cannot save instance {instance.name} - don't know original image name")

		# Try to copy the version number of the image we're based on.
		# If that fails for some reason, be lame and use 'latest'.
		version = 'latest'
		if ':' in instance.image:
			version = instance.image.split(':')[1]

		outputName = f"{platform.name}:{version}"
		outputImage = ImageReference("localhost", outputName)

		existingImage = self.findImage(outputImage)

		# FIXME: we should probably remove most if not all of the labels
		# and replace them with something more a propos

		cmd = f"sudo podman commit {instance.containerId} {outputImage}"
		status = self.runShellCmd(cmd, cwd = instance.workspace, timeout = 120)
		if not status:
			raise ValueError(f"{instance.name}: podman package failed {status}")

		info(f"Created image {outputImage}")

		if existingImage:
			self.runShellCmd(f"sudo podman rmi {existingImage.id}")

		# Inside the platform {} decl, create backend specific info:
		#	backend podman {
		#		image "leap-15.4-container-fips-twopence:version";
		#	}
		platform.addBackend(self.name, image = outputName)

		# Clear any cached image listing
		self.listing = None

	def packageInstance(self, instance, platform):
		self.saveInstanceImage(instance, platform)

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
			raise ValueError("podman: could not list images")

		listing = ContainerImageListing()
		for entry in data:
			config = listing.addEntryJSON(entry)

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

