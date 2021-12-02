include Make.defs

BINDIR	= /usr/bin
ETCDIR	= /etc
LIBDIR	= /usr/lib

TWP_ETCDIR	= $(TWOPENCE_ETCDIR)
TWP_BINDIR	= $(TWOPENCE_BINDIR)
TWP_LIBDIR	= $(LIBDIR)/twopence/provision
TWP_PYDIR	= $(PYTHON_INSTDIR)/_twopence/provision

install:
	mkdir -p $(DESTDIR)$(BINDIR)
	mkdir -p $(DESTDIR)$(TWP_ETCDIR)
	mkdir -p $(DESTDIR)$(TWP_LIBDIR)
	mkdir -p $(DESTDIR)$(TWP_PYDIR)
	install -m 555 twopence-provision $(DESTDIR)$(TWP_BINDIR)/provision
	install -m 644 provision.conf $(DESTDIR)$(TWP_ETCDIR)
	install -m 444 templates/Vagrantfile.in $(DESTDIR)$(TWP_LIBDIR)
	cp -vr python/_twopence/* $(DESTDIR)$(TWP_PYDIR)
