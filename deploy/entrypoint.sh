#!/bin/sh
set -eu
umask 0007
mkdir -p /data/profiles
chown app:app /data
chmod 0711 /data
chown app:browser /data/profiles
chmod 2770 /data/profiles

# Fail closed if rules cannot be installed. The browser UID may contact only the
# checked egress proxy; established CDP replies remain usable. DNS resolution is
# done by the proxy, not the browser. X11 uses a Unix socket, not TCP.
proxy_ip="$(getent ahostsv4 egress | awk 'NR==1 {print $1}')"
test -n "$proxy_ip"
iptables -N CB_BROWSER
iptables -A OUTPUT -m owner --uid-owner 1001 -j CB_BROWSER
iptables -A CB_BROWSER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A CB_BROWSER -p tcp -d "$proxy_ip" --dport 3128 -j ACCEPT
iptables -A CB_BROWSER -j REJECT
ip6tables -N CB_BROWSER6
ip6tables -A OUTPUT -m owner --uid-owner 1001 -j CB_BROWSER6
ip6tables -A CB_BROWSER6 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A CB_BROWSER6 -j REJECT
export CB_NETWORK_ISOLATED=true
export CB_BROWSER_PROXY="http://$proxy_ip:3128"

# Display and private control services are started by the shared worker only
# while a session/control lease exists. X11 uses a protected authority cookie.
mkdir -p /run/cloud-browser
chown app:browser /run/cloud-browser
chmod 0750 /run/cloud-browser
export CB_MANAGED_DISPLAY=true
export CB_RUNTIME_DIR=/run/cloud-browser
export CB_BROWSER_CLEANUP_COMMAND=/usr/local/bin/cloud-browser-stop
exec setpriv --reuid=app --regid=app --init-groups cloud-browser serve
