FROM alpine:3.21
# chan_dongle allows sending SMS through a USB GSM dongle (optional).
RUN apk add --no-cache asterisk asterisk-chan-dongle gettext tzdata
COPY conf/ /etc/asterisk/templates/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /var/lib/asterisk/sounds/alarm
EXPOSE 5060/udp
ENTRYPOINT ["/entrypoint.sh"]
