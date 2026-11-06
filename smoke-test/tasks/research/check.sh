set -e
f=answers.md
test -f $f
grep -q '\$15' $f
grep -q '15%' $f
grep -qi 'business' $f
grep -q '\$29' $f
grep -q '2024-09-01' $f
