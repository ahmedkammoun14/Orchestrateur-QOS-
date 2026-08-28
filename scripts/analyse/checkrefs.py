import re, sys, collections

tex = open(sys.argv[1], encoding='utf-8').read()
labels = re.findall(r'\\label\{([^}]*)\}', tex)
refs = set(re.findall(r'\\ref\{([^}]*)\}', tex))

dupes = [l for l, c in collections.Counter(labels).items() if c > 1]
print('labels          :', len(labels))
print('DOUBLONS        :', dupes or 'aucun')
print('refs sans label :', sorted(refs - set(labels)) or 'aucune')
unused = sorted(set(labels) - refs)
print('labels non cités:', unused or 'aucun')
