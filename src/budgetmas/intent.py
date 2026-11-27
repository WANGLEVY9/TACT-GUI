"""Compile explicit GUI instructions and ground permitted clicks at runtime."""
from dataclasses import asdict, dataclass
import json
import re
import unicodedata
from typing import Optional

from .ui_nodes import labels, node_bounds, tap_location


QUOTED = r'["“](?P<label>[^"“”\r\n]{1,96})["”]'
CLICK = re.compile(
    rf'(?:click(?: on)?(?: the)?(?P<pre_button> button)?\s+{QUOTED}(?P<post_button> button)?'
    r'|click button (?P<identifier>[A-Za-z0-9_-]{1,32})'
    r'|点击\s*["“](?P<zh>[^"“”\r\n]{1,96})["”](?P<zh_button>按钮)?)', re.I)
LINK = re.compile(rf'click on the link {QUOTED}', re.I)
FORBID = re.compile(rf"(?:do not click|don't click|不要点击|请勿点击)\s*{QUOTED}", re.I)
CLOSE_DIALOG = re.compile(r'close the dialog box by clicking the ["“]x["”]', re.I)
CLAUSE = re.compile(r'\s*(?:[.!。；;]+|[,，]\s*(?:then\b|然后|再)|\bthen\b|然后|再点击)\s*', re.I)


def normalize(label):
    return ' '.join(unicodedata.normalize('NFKC', label).casefold().split())


@dataclass(frozen=True)
class Step:
    target: str
    role: Optional[str] = None


@dataclass(frozen=True)
class Intent:
    steps: tuple[Step, ...]
    forbidden: tuple[str, ...]

    def describe(self):
        return {'steps':[asdict(step) for step in self.steps], 'forbidden':list(self.forbidden)}


def compile_intent(instruction):
    """Recognize a closed grammar. Every clause must match, including constraints."""
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        return None
    # Preserve delimiters inside quoted control labels.
    chunks = re.split(r'(["“][^"“”\r\n]+["”])', instruction.strip())
    clauses = ['']
    for i, chunk in enumerate(chunks):
        if i % 2:
            clauses[-1] += chunk
            continue
        parts = re.split(CLAUSE, chunk.replace('再点击', '然后点击'))
        clauses[-1] += parts[0]
        clauses.extend(parts[1:])
    steps, forbidden = [], []
    constraints_started = False
    for clause in (c.strip() for c in clauses if c.strip()):
        if match := FORBID.fullmatch(clause):
            forbidden.append(match['label'])
            constraints_started = True
            continue
        if constraints_started:
            return None
        if match := CLICK.fullmatch(clause):
            role = 'button' if any(match[g] for g in ('pre_button','post_button','zh_button','identifier')) else None
            steps.append(Step(match['label'] or match['identifier'] or match['zh'], role))
        elif match := LINK.fullmatch(clause):
            steps.append(Step(match['label'], 'link'))
        elif CLOSE_DIALOG.fullmatch(clause):
            steps.append(Step('Close', 'button'))
        else:
            return None
    if not steps or any(not normalize(step.target) or normalize(step.target) in
                        {normalize(label) for label in forbidden} for step in steps):
        return None
    return Intent(tuple(steps), tuple(forbidden))


def control_role(node):
    role = str(node.get('role', '')).lower()
    if role in ('button','link'):
        return role
    if role == 'a':
        return 'link'
    name = str(node.get('class', '')).rsplit('.', 1)[-1].lower()
    return 'button' if name in ('button','imagebutton') else None


def ground(step, forbidden, nodes, width, height):
    """Return uniquely grounded, usable pixel coordinates and a decision reason."""
    blocked = {normalize(label) for label in forbidden}
    points = {}
    target = normalize(step.target)
    clickable = []
    for node in nodes:
        if (str(node.get('enabled','true')).lower()=='false'
                or str(node.get('clickable','false')).lower()!='true' or node.get('center_hit') is False):
            continue
        names = {normalize(label) for label in labels(node)}
        clickable.append((node,names))
        if target not in names or names & blocked:
            continue
        if step.role and control_role(node) != step.role:
            continue
        point = tap_location(node)
        if point and 0 <= point[0] < width and 0 <= point[1] < height:
            points[point] = node
    if len(points) != 1:
        return None, 'ambiguous_target' if points else 'missing_target'
    (x,y), node = next(iter(points.items()))
    if node.get('center_hit') is None:
        for other,names in clickable:
            if other is node or target in names:
                continue
            box = node_bounds(other)
            if box and box[0]<=x<box[2] and box[1]<=y<box[3]:
                return None, 'overlapping_clickable_controls'
    return (x,y), 'unique_grounded_target'


def semantic_candidate(instruction, answer):
    """Validate a text model's reviewable interpretation and evidence citations."""
    draft = json.loads(answer)
    if not isinstance(draft,dict) or set(draft)!={'steps','constraints','question'}:
        raise ValueError('unexpected semantic contract fields')
    for key in ('steps','constraints'):
        if not isinstance(draft[key],list) or len(draft[key])>8:
            raise ValueError('invalid semantic contract list')
        for item in draft[key]:
            if (not isinstance(item,dict) or set(item)!={'meaning','evidence'}
                    or not all(isinstance(v,str) and 0<len(v.strip())<=200 for v in item.values())
                    or item['evidence'] not in instruction):
                raise ValueError('semantic contract item has no cited instruction evidence')
    question = draft['question']
    if question is not None and (not isinstance(question,str) or not 0<len(question.strip())<=240):
        raise ValueError('invalid clarification question')
    return draft
