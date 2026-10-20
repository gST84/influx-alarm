#!/bin/sh
# Renders the Asterisk configuration from environment variables, then starts Asterisk.
set -eu

: "${AMI_USER:=alarm}"
: "${AMI_PASSWORD:?AMI_PASSWORD must be set}"
: "${SIP_HOST:=}"
: "${SIP_USERNAME:=}"
: "${SIP_PASSWORD:=}"
: "${SIP_AUTH_USERNAME:=$SIP_USERNAME}"
: "${SIP_PORT:=5060}"
: "${SIP_TRANSPORT:=udp}"
: "${SIP_FROM_DOMAIN:=$SIP_HOST}"
: "${EXTERNAL_IP:=}"
: "${RTP_START:=10000}"
: "${RTP_END:=10100}"
export AMI_USER AMI_PASSWORD SIP_HOST SIP_USERNAME SIP_PASSWORD SIP_AUTH_USERNAME SIP_PORT \
  SIP_TRANSPORT SIP_FROM_DOMAIN EXTERNAL_IP RTP_START RTP_END

# NAT: tell the provider our public address when one is given.
if [ -n "$EXTERNAL_IP" ]; then
  export NAT_LINES="external_media_address=$EXTERNAL_IP
external_signaling_address=$EXTERNAL_IP
local_net=10.0.0.0/8
local_net=172.16.0.0/12
local_net=192.168.0.0/16"
else
  export NAT_LINES=""
fi

# Only our own variables: dialplan variables like ${ALARM_SOUND} must stay as they are.
VARS='$AMI_USER $AMI_PASSWORD $SIP_HOST $SIP_USERNAME $SIP_PASSWORD $SIP_AUTH_USERNAME $SIP_PORT $SIP_TRANSPORT $SIP_FROM_DOMAIN $RTP_START $RTP_END $NAT_LINES'
for f in /etc/asterisk/templates/*.conf; do
  envsubst "$VARS" < "$f" > "/etc/asterisk/$(basename "$f")"
done

# Without SIP credentials only the test numbers (test:1001 …) work.
if [ -z "$SIP_HOST" ] || [ -z "$SIP_USERNAME" ]; then
  echo "SIP_HOST/SIP_USERNAME not set: trunk disabled, only test calls are possible"
  sed -i '/;; TRUNK START/,/;; TRUNK END/d' /etc/asterisk/pjsip.conf
fi

mkdir -p /var/lib/asterisk/sounds/alarm
chown -R asterisk:asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk 2>/dev/null || true
exec asterisk -f -U asterisk -G asterisk -vvv
