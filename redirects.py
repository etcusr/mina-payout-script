"""Exact payout redirection; invalid configuration fails closed."""
from fractions import Fraction
from money import parse_decimal

def parse(raw,warn=None):
    if raw is None: return {}
    if not isinstance(raw,dict): raise ValueError('redirects must be a mapping')
    result={}
    for source,target in sorted(raw.items()):
        if not isinstance(source,str) or not source: raise ValueError('invalid redirect source')
        target={target:1} if isinstance(target,str) else target
        if not isinstance(target,dict) or not target: raise ValueError('redirect requires destinations')
        parts=[]
        for destination,weight in sorted(target.items()):
            value=Fraction(parse_decimal(weight))
            if not isinstance(destination,str) or not destination or value<=0: raise ValueError('invalid redirect destination or weight')
            parts.append((destination,value))
        total=sum(w for _,w in parts)
        result[source]=[(a,w/total) for a,w in parts]
    return result

def split(total_amount,targets):
    if type(total_amount) is not int or total_amount<0 or not targets: raise ValueError('redirect amount requires nonnegative integer nanomina')
    assigned=0; result=[]
    for index,(address,fraction) in enumerate(targets):
        value=total_amount-assigned if index==len(targets)-1 else int(total_amount*Fraction(fraction))
        assigned+=value; result.append((address,value))
    return result

def describe(parsed):
    return [line for source,targets in parsed.items() for line in [source]+[f'    -> {dst}  {float(fraction)*100:g}%' for dst,fraction in targets]]
