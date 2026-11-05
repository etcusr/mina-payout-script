"""Exact, deterministic reward allocation. No network or filesystem effects."""
import hashlib
from fractions import Fraction
from money import parse_decimal, mina_to_nano
import redirects

RATES=('VALIDATOR_FEE','VALIDATOR_FEE_SP','VALIDATOR_FEE_FOUNDATION','VALIDATOR_FEE_O1LABS')

def validate_address(address):
    """Validate Mina public key Base58Check encoding, not ownership."""
    if not isinstance(address,str) or len(address)!=55:
        raise ValueError('invalid Mina public key')
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    try:
        number=0
        for char in address: number=number*58+alphabet.index(char)
        raw=number.to_bytes((number.bit_length()+7)//8,'big')
        if len(raw)!=40 or raw[:3]!=bytes([203,1,1]) or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4]!=raw[-4:]:
            raise ValueError()
    except (ValueError,OverflowError): raise ValueError('invalid Mina public key checksum or encoding') from None
    return address

def integer(value, name='amount', minimum=0):
    if type(value) is not int or value<minimum or value>=2**64:
        raise ValueError(name+' must be an integer in the allowed range')
    return value

def policy(config,era,epoch):
    result={key:config.get(key,'0') for key in RATES}
    schedule=config.get('COMMISSION_SCHEDULE')
    if schedule is not None:
        eligible=[]; seen=set()
        for row in schedule:
            start=int(row['from_epoch']); row_era=str(row['era'])
            if start<0 or (row_era,start) in seen: raise ValueError('invalid commission schedule')
            seen.add((row_era,start))
            if row_era==str(era) and start<=epoch: eligible.append(row)
        if not eligible: raise ValueError('commission schedule does not cover requested era/epoch')
        for row in sorted(eligible,key=lambda r:int(r['from_epoch'])):
            result.update({key:row[key] for key in RATES if key in row})
    for key,value in result.items():
        rate=parse_decimal(value)
        if not 0<=rate<=1: raise ValueError('commission must be between zero and one')
        result[key]=str(rate)
    result['minimum_nano']=mina_to_nano(config.get('MINIMUM_PAYOUT','0'))
    result['fee_nano']=integer(config.get('DEFAULT_TX_FEE',0),'fee')
    result['exclusions']=sorted(config.get('EXCLUDE_FROM_PAYOUTS',[config['SEND_FROM_ADDRESS'],config['VALIDATOR_ADDRESS']]))
    result['redirects']={src:[(dst,str(weight)) for dst,weight in targets] for src,targets in redirects.parse(config.get('PAYOUT_REDIRECTS')).items()}
    result['rounding']='floor each staking allocation; last sorted redirect receives remainder; validator retains subnano allocation remainder'
    for a in result['exclusions']: validate_address(a)
    return result

def calculate(config,era,epoch,stakes,blocks,*,base_coinbase_nano,carry,foundation=(),labs=()):
    p=policy(config,era,epoch); integer(base_coinbase_nano,'base coinbase',1)
    normal=extra=retained=total=0
    last_slot=max((b['slot'] for b in blocks),default=0)
    special=set(foundation)|set(labs)
    for b in blocks:
        reward=integer(b['reward_nano']); snark=integer(b['snark_nano']); fees=integer(b['fees_nano'])
        total+=reward+fees-snark
        surplus=max(0,reward-base_coinbase_nano)
        normal+=reward-surplus+fees-snark
        if b['winner'] in special: retained+=surplus
        else: extra+=surplus
    if normal<0: raise ValueError('negative distributable reward')
    weights={}; unlocked={}
    for s in stakes:
        a=validate_address(s['public_key']); balance=Fraction(parse_decimal(s['balance']))
        if a in weights or balance<0: raise ValueError('duplicate or negative staking balance')
        weights[a]=balance
        if not s.get('timing') or int(s['timing']['untimed_slot'])<=last_slot: unlocked[a]=balance
    denominator=sum(weights.values()); unlocked_total=sum(unlocked.values())
    if total and not denominator: raise ValueError('reward has no staking denominator')
    gross={}; excluded_by_address={}; allocated=excluded=0
    targets=redirects.parse(config.get('PAYOUT_REDIRECTS'))
    for a,weight in sorted(weights.items()):
        rate=p['VALIDATOR_FEE_FOUNDATION'] if a in foundation else p['VALIDATOR_FEE_O1LABS'] if a in labs else p['VALIDATOR_FEE']
        reward=Fraction(normal)*weight/denominator*(1-Fraction(rate)) if denominator else Fraction(0)
        if a not in special and a in unlocked and unlocked_total:
            reward+=Fraction(extra)*weight/unlocked_total*(1-Fraction(p['VALIDATOR_FEE_SP']))
        amount=reward.numerator//reward.denominator; allocated+=amount
        for dst,value in redirects.split(amount,targets.get(a,[(a,Fraction(1))])):
            validate_address(dst)
            if dst in p['exclusions']:
                excluded+=value
                excluded_by_address[dst]=excluded_by_address.get(dst,0)+value
            else: gross[dst]=gross.get(dst,0)+value
    net={}; after={}
    for a in sorted(set(gross)|set(carry)):
        validate_address(a)
        if type(carry.get(a,0)) is not int: raise ValueError('carry must be integer nanomina')
        owed=gross.get(a,0)+carry.get(a,0)
        amount=owed if owed>0 and owed>=p['minimum_nano'] else 0
        if amount: net[a]=amount
        if owed-amount: after[a]=owed-amount
    return {'gross_nano':gross,'net_nano':net,'carry_before_nano':dict(carry),'carry_after_nano':after,
            'validator_nano':total-allocated,'excluded_nano':excluded,'excluded_by_address_nano':excluded_by_address,'total_reward_nano':total,'policy':p}
