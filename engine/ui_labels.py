"""Semantic menu actions with captions for supported Music UI languages."""
import json
from pathlib import Path

LABELS_PATH=Path(__file__).with_name('menu_labels.json')

def menu_labels(action):
    labels=json.loads(LABELS_PATH.read_text(encoding='utf-8'))
    if action not in labels:
        raise ValueError('Unknown Music menu action: '+action)
    names=labels[action]
    if not isinstance(names,list) or not names or not all(isinstance(n,str) and n.strip() for n in names):
        raise ValueError('Invalid menu labels for '+action)
    return names
