set -e
python3 -m pytest -q
printf 'the cat the dog\nThe end\n' > /tmp/_wc_in.txt
out=$(python3 wordcount.py --top 2 /tmp/_wc_in.txt)
echo "$out" | grep -q "^the 3$"
echo "$out" | sed -n 2p | grep -Eq "^(cat|dog|end) 1$"
grep -q "top" test_wordcount.py
