##################################################################
#
# Minimal logging stuff
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################


opt_verbose = 1

def setVerbosity(num):
	global opt_verbose

	opt_verbose = num

def debug_enabled():
	return opt_verbose >= 2

def verbose_enabled():
	return opt_verbose >= 1

def debug(msg):
	if opt_verbose >= 2:
		print("DEBUG: %s" % msg)

def verbose(msg):
	if opt_verbose >= 1:
		print("%s" % msg)
