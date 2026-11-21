FROM alpine:3.22

RUN apk add --no-cache squid ca-certificates \
    && mkdir -p /tmp/squid \
    && chown squid:squid /tmp/squid

COPY egress-proxy/squid.conf /etc/squid/squid.conf

USER squid
EXPOSE 3128
ENTRYPOINT ["squid", "-N", "-f", "/etc/squid/squid.conf"]
