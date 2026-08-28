import re, sys

tex = open(sys.argv[1], encoding='utf-8').read()
bib = open(sys.argv[2], encoding='utf-8').read()

used = set()
for m in re.findall(r'\\cite\{([^}]*)\}', tex):
    used |= {k.strip() for k in m.split(',')}
defined = set(re.findall(r'@\w+\{([^,]+),', bib))

print('cited keys :', len(used))
print('bib entries:', len(defined))
print('MISSING (cited, no entry) :', sorted(used - defined) or 'none')
print('UNUSED  (entry, no cite)  :', sorted(defined - used) or 'none')
