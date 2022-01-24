##################################################################
#
# Backed base class. Currently not very beefy...
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .config import Configurable

class Backend(Configurable):
	def __init__(self):
		super().__init__()
		# By default, auto-update images that we get from remote
		self.auto_update = True

	@staticmethod
	def create(family):
		if family == 'vagrant':
			from .vagrant import VagrantBackend

			return VagrantBackend()

		raise ConfigError("Cannot create backend \"%s\" - unknown backend family" % family)

	# Return a list of name/value pairs describing the image associated with
	# a platform.
	# The info argument is a Config.SavedBackendConfig object
	def renderPlatformInformation(self, info):
		return []
