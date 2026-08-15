"""Pachetul nomenclator (validatorul „bogat" + glue). `address_nomenclator.py` e VERBATIM din sursa menținută
(xconnector/address_nomenclator.py) și face `import address_rules` absolut — punem dir-ul pachetului pe sys.path
ca importul să se rezolve fără să edităm validatorul (păstrăm sync-ul verbatim cu sursa)."""
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)
