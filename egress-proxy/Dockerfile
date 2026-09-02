FROM alpine:3.20

# addgroup/adduser tolerate "already exists" (|| true): the apk
# package may or may not have created a "squid" service account
# itself depending on the base image, and this makes the Dockerfile
# correct either way instead of depending on which. mkdir + chown are
# explicit rather than relying on the package's own post-install
# setup, since Alpine's apk doesn't have as consistent a postinstall-
# hook convention as Debian's dpkg. squid.conf's cache_effective_user
# is pinned to this exact name — squid hard-refuses to run as root,
# so this account has to exist and own its runtime directories for
# the container to start at all.
RUN apk add --no-cache squid \
    && (addgroup -S squid || true) \
    && (adduser -S -D -H -G squid squid || true) \
    && mkdir -p /var/spool/squid /var/log/squid \
    && chown -R squid:squid /var/spool/squid /var/log/squid

COPY squid.conf /etc/squid/squid.conf
COPY allowed-internal-hosts.txt /etc/squid/allowed-internal-hosts.txt

EXPOSE 3128

# -N: stay in the foreground (required for Docker's process model,
# squid daemonizes by default). -d 1: basic request-level logging to
# stderr so `docker compose logs guard-proxy` shows what's happening.
CMD ["squid", "-N", "-d", "1"]
