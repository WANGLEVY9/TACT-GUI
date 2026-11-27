"""Platform-neutral visible UI node contract shared by Android and Web.

Oracle definitions are evaluation-only. They may use any subset of these
fields, but never platform-specific identifiers such as APK resource IDs.
"""
import hashlib
import re


FIELDS = ('resource', 'text', 'description', 'role', 'name', 'selected')
BOOLEAN_FIELDS = ('selected',)

ORACLE_FIELDS = ('text', 'description', 'selected', 'role', 'name')


def sample(node, field):
    if field == 'resource':
        identifier = node.get('resource-id', '')
        return identifier.rsplit('/', 1)[-1]
    if field == 'description':
        return node.get('content-desc', '')
    if field == 'role':
        return node.get('role', '')
    if field == 'name':
        return node.get('name', '')
    if field == 'selected':
        return node.get('aria-selected', node.get('selected', ''))
    return node.get(field, '')


def matches(nodes, oracle):
    if not isinstance(oracle, dict) or not oracle:
        return False
    unexpected = set(oracle) - set(ORACLE_FIELDS) - {'resource'}
    if unexpected:
        raise ValueError('unknown oracle selector')
    for node in nodes:
        if 'resource' in oracle and sample(node, 'resource') != oracle['resource']:
            continue
        ok = True
        for field in ('text', 'description', 'role', 'name', 'selected'):
            if field not in oracle:
                continue
            value = (node.get('selected', '') or sample(node, field) if field == 'selected'
                     else sample(node, field))
            expected = oracle[field]
            if field == 'selected':
                ok = str(value).lower() == str(expected).lower() and str(value).lower() in ('true', 'false')
            else:
                ok = str(value) == str(expected)
            if not ok:
                break
        if ok:
            return True
    return False


def state_hash(nodes, domain):
    """Hash structural/identity fields only; dynamic text and time are omitted."""
    structure = []
    for node in nodes:
        owner = node.get('domain') or node.get('package')
        if owner not in (None, domain):
            continue
        structure.append((node.get('resource-id', ''), node.get('class', ''),
                          node.get('selected', ''), node.get('checked', ''),
                          node.get('bounds', '')))
    if not structure:
        raise RuntimeError('configured application has no visible nodes')
    return hashlib.sha256(repr(structure).encode()).hexdigest()


def ui_features(nodes, domain):
    visible = [node for node in nodes
               if (node.get('domain') or node.get('package')) in (None, domain)]
    if not visible:
        raise RuntimeError('empty decision observation')
    labelled = sum(bool(sample(node, 'text') or sample(node, 'description') or sample(node, 'name'))
                   for node in visible)
    clickable = sum(str(node.get('clickable', '')).lower() == 'true' for node in visible)
    labels = [str(sample(node, 'text') or sample(node, 'description') or sample(node, 'name')).casefold()
              for node in visible]
    labels = [label for label in labels if label]
    total = len(visible)
    return {'nodes': total, 'labelled_nodes': labelled, 'clickable_nodes': clickable,
            'duplicate_visible_labels': len(labels) - len(set(labels))}


def inspect_checkpoint(nodes, domain, case, *, oracle_nodes=None):
    observed = nodes if oracle_nodes is None else oracle_nodes
    if not matches(observed, case['start']) or matches(observed, case['oracle']):
        raise RuntimeError('registered checkpoint assertions failed')
    features = ui_features(nodes, domain)
    if case.get('match_visible_state'):
        public = [(labels(n), n.get('enabled',''), n.get('clickable',''))
                  for n in nodes if (n.get('domain') or n.get('package')) in (None, domain)]
        features['decision_observation_sha256'] = hashlib.sha256(repr(public).encode()).hexdigest()
    return state_hash(nodes, domain), features


def labels(node):
    return tuple(str(node.get(key, '') or '') for key in
                 ('text', 'content-desc', 'aria-label', 'name', 'label', 'data-label'))


def node_bounds(node):
    bounds = node.get('bounds', '')
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        left, top, right, bottom = bounds
    else:
        match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', str(bounds))
        if not match:
            return None
        left, top, right, bottom = map(int, match.groups())
    if not left < right or not top < bottom:
        return None
    return left, top, right, bottom


def tap_location(node):
    bounds = node_bounds(node)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def find_target(nodes, phrases):
    """Locate a visible control by one of its registered labels.

    Returns a pixel centre or None. Both XML Element nodes and Web dict nodes
    expose .get, so the same locator drives prefixes on either platform.
    """
    for phrase in phrases:
        eligible = [node for node in nodes
                    if phrase.casefold() in {label.casefold() for label in labels(node) if label}
                    and tap_location(node)
                    and str(node.get('enabled', 'true')).lower() != 'false']
        if eligible:
            preferred = max(eligible, key=lambda node: bool(node.get('clickable')))
            return tap_location(preferred)
    return None
