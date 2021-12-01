##################################################################
#
# Backed base class. Currently not very beefy...
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .config import Configurable

class Backend(Configurable):
	@staticmethod
	def create(family):
		if family == 'vagrant':
			from .vagrant import VagrantBackend

			return VagrantBackend()

		raise ConfigError("Cannot create backend \"%s\" - unknown backend family" % family)
