import json
p = '/home/dayodapper/kingdom/kingdom_state.json'
s = json.load(open(p))
s.setdefault('position_registry', {})['augur'] = []
s.setdefault('augur', {})['open_positions'] = 0
open(p, 'w').write(json.dumps(s, indent=2))
print('Cleared. augur registry:', s['position_registry']['augur'])
print('augur open_positions:', s['augur']['open_positions'])
