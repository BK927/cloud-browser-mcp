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

Xvfb :99 -screen 0 1920x1440x24 -nolisten tcp -ac &
xvfb_pid=$!
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if [ -S /tmp/.X11-unix/X99 ]; then break; fi
    sleep 1
done
test -S /tmp/.X11-unix/X99
cleanup() {
    kill "${app_pid:-}" "${vnc_pid:-}" "${ws_pid:-}" "$xvfb_pid" 2>/dev/null || true
    wait || true
}
trap cleanup EXIT INT TERM
if [ "${CB_MANUAL_CONTROL_ENABLED:-false}" = "true" ]; then
    x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever -shared -noxdamage -quiet -o /dev/null &
    vnc_pid=$!
    websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/dev/null 2>&1 &
    ws_pid=$!
fi
setpriv --reuid=app --regid=app --init-groups cloud-browser serve &
app_pid=$!
wait "$app_pid"
