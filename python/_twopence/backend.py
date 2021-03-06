##################################################################
#
# Backed base class. Currently not very beefy...
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from twopence import ConfigError
from .config import Configurable

class Backend(Configurable):
	def __init__(self):
		super().__init__()

		# Do not automatically update images that we get from remote
		self.auto_update = False

		self.testcase = None

	@staticmethod
	def create(family):
		if family == 'vagrant':
			from .vagrant import VagrantBackend

			return VagrantBackend()

		if family == 'podman':
			from .podman import PodmanBackend

			return PodmanBackend()

		raise ConfigError("Cannot create backend \"%s\" - unknown backend family" % family)

	# Return a list of name/value pairs describing the image associated with
	# a platform.
	# The info argument is a Config.SavedBackendConfig object
	def renderPlatformInformation(self, info):
		return []

	def prepareApplication(self, instance):
		raise NotImplementedError(f"The {self.name} backend does not support application images")

	def destroyVolume(self, volumeID):
		pass
