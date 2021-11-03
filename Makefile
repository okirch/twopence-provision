BINDIR	= /usr/bin
ETCDIR	= /etc
LIBDIR	= /usr/lib

TWP_ETCDIR	= $(ETCDIR)/twopence
TWP_LIBDIR	= $(LIBDIR)/twopence/provision

install:
	mkdir -p $(DESTDIR)$(BINDIR)
	mkdir -p $(DESTDIR)$(TWP_ETCDIR)
	mkdir -p $(DESTDIR)$(TWP_LIBDIR)
	install -m 555 twopence-provision $(DESTDIR)$(BINDIR)/twopence-provision
	install -m 644 provision.conf $(DESTDIR)$(TWP_ETCDIR)
	install -m 444 Vagrantfile.in $(DESTDIR)$(TWP_LIBDIR)
