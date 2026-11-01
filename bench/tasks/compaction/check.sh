set -e
test -f timeouts.md
for i in 1 2 3 4 5 6; do grep -q "TIMEOUT_SECONDS_$i" timeouts.md; done
grep -q "147" timeouts.md
