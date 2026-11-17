"""SQL-shaped archive records; no signing, production writes or key access."""
import copy
import hashlib
import unittest
import tempfile
from unittest.mock import patch

import preflight
import batch_archive

CONFIG = {'EXPECTED_CHAIN_ID': 'network', 'SEND_FROM_ADDRESS': 'wallet'}
EXPECTED = {'network':'network', 'wallet':'wallet', 'nonce':7, 'tx_hash':'tx',
            'fee_nano':1000000, 'memo':'batch_test', 'recipients':[
                {'recipient':'alice','amount_nano':1}, {'recipient':'bob','amount_nano':2}]}
TOKEN = 'wSHV2S4qX9jFsLjQo8r1BsMLH2ZRKsZx6EJd1sbozGPieEC4Jf'


def memo(text,padding=0):
    body=bytes([20,1,len(text.encode())])+text.encode()+bytes([padding])*(32-len(text.encode()))
    raw=body+hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    number=int.from_bytes(raw,'big'); result=''
    while number:
        number,remainder=divmod(number,58)
        result='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'[remainder]+result
    return result


def nullable(**values):
    return dict(id=1,**values)


def update(index,address,amount):
    return {'ordinality':index+1,'update_id':100+index,'address':address,'token':TOKEN,
        'body':{'id':900+index,'account_identifier_id':10+index,'update_id':1,
            'balance_change':str(amount),'increment_nonce':False,'events_id':None,'actions_id':None,
            'call_data_id':1,'call_depth':0 if index==0 else 1,
            'zkapp_network_precondition_id':1,'zkapp_account_precondition_id':1,
            'zkapp_valid_while_precondition_id':None,'use_full_commitment':index==0,
            'implicit_account_creation_fee':False,'may_use_token':'No',
            'authorization_kind':'Signature' if index==0 else 'None_given','verification_key_hash_id':None},
        'changes':nullable(app_state_id=1,delegate_id=None,verification_key_id=None,permissions_id=None,
            zkapp_uri_id=None,token_symbol_id=None,timing_id=None,voting_for_id=None),
        'app_state':nullable(**{'element'+str(i):None for i in range(32)}),
        'account_precondition':nullable(balance_id=None,nonce_id=None,receipt_chain_hash=None,
            delegate_id=None,state_id=1,action_state_id=None,proved_state=None,is_new=False),
        'account_state':nullable(**{'element'+str(i):None for i in range(32)}),
        'network_precondition':nullable(snarked_ledger_hash_id=None,blockchain_length_id=None,
            min_window_density_id=None,total_currency_id=None,global_slot_since_genesis=None,
            staking_epoch_data_id=1,next_epoch_data_id=1),
        'staking_epoch':nullable(epoch_ledger_id=1,epoch_seed=None,start_checkpoint=None,
            lock_checkpoint=None,epoch_length_id=None),
        'next_epoch':nullable(epoch_ledger_id=1,epoch_seed=None,start_checkpoint=None,
            lock_checkpoint=None,epoch_length_id=None),
        'staking_ledger':nullable(hash_id=None,total_currency_id=None),
        'next_ledger':nullable(hash_id=None,total_currency_id=None),
        'events':None,'actions':None,'call_data':'0'}


class Connection:
    def set_session(self,**kwargs): self.session=kwargs
    def close(self): self.closed=True


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.connection=Connection()
        self.command={'command_id':11,'tx_hash':'tx','wallet':'wallet','fee_nano':'1000000',
            'nonce':7,'memo':memo('batch_test'),'valid_until':None,'update_ids':[100,101,102],
            'block_id':20,'block_hash':'h20','height':20,'era':3,'chain_status':'canonical',
            'in_chain_status':'applied','failure_reasons_ids':None,'sequence_no':3}
        self.commands=[self.command]
        self.updates=[update(0,'wallet',-3),update(1,'alice',1),update(2,'bob',2)]
        self.created=[]
        self.health={'ok':True,'network':'network','tip_hash':'h800','tip_height':800,
            'best_chain':[{'id':20,'state_hash':'h20','height':20,'protocol_version_id':3,
                           'chain_status':'canonical','depth':780}]}
        self.calls=[]

    def rows(self,connection,query,params=None):
        self.calls.append(query)
        if "set_config('statement_timeout'" in query:return [{}]
        if 'FROM zkapp_commands z' in query:return copy.deepcopy(self.commands)
        if 'WITH ORDINALITY' in query:return copy.deepcopy(self.updates)
        if 'FROM accounts_created' in query:return copy.deepcopy(self.created)
        raise AssertionError('Unexpected query: '+query)

    def verify(self,**kwargs):
        with patch.object(preflight,'_connect',return_value=self.connection),\
             patch.object(preflight,'_rows',side_effect=self.rows),\
             patch.object(preflight,'check',return_value=self.health) as health:
            result=batch_archive.verify(CONFIG,EXPECTED,**kwargs)
        self.health_call=health.call_args
        return result

    def test_old_inclusion_proves_exact_ordered_payload(self):
        result=self.verify()
        self.assertEqual(result['status'],'applied')
        self.assertEqual(result['depth'],780)
        self.assertTrue(result['on_best_chain'])
        self.assertEqual((result['paid_nano'],result['fee_paid_nano']),(3,1000000))
        for key,value in EXPECTED.items():self.assertEqual(result[key],value)
        self.assertEqual(result['era'],'3')
        self.assertEqual(self.health_call.kwargs,{'require_wallet':False,'min_height':20})
        self.assertEqual(self.connection.session,{'readonly':True,'autocommit':False,'isolation_level':'REPEATABLE READ'})
        self.assertTrue(self.connection.closed)
        sql=next(q for q in self.calls if 'WITH ORDINALITY' in q)
        self.assertIn('zkapp_account_update a ON a.id=',sql)
        self.assertIn('b.id=a.body_id',sql)

    def test_persisted_plan_archive_proof_settles_once(self):
        from batch_accounting import BatchStore
        from tests.test_batch_accounting import fixture
        plan=fixture(network='network',fee_nano=1000000)
        plan['recipients']=plan['recipients'][:2]
        self.command['wallet']=plan['wallet']
        self.updates=[update(0,plan['wallet'],-3)]+[
            update(i+1,r['recipient'],r['amount_nano']) for i,r in enumerate(plan['recipients'])]
        with tempfile.TemporaryDirectory() as directory:
            store=BatchStore(directory);digest=store.create_plan(plan)
            store.prepare(digest,0,7,{'fixture':'signed'},'tx');store.claim(digest,0)
            with patch.object(preflight,'_connect',return_value=self.connection),\
                 patch.object(preflight,'_rows',side_effect=self.rows),\
                 patch.object(preflight,'check',return_value=self.health):
                proof=batch_archive.verify(dict(CONFIG,SEND_FROM_ADDRESS=plan['wallet']),store.expected(digest,0))
            self.assertTrue(store.observe(digest,0,proof))
            self.assertFalse(store.observe(digest,0,proof))
            state=store.snapshot(plan['wallet'],plan['network'])
            self.assertEqual((state['spent_nano'],state['reserved_nano']),(1000003,0))

    def test_below_required_depth_pending_even_if_failed(self):
        self.health['tip_height']=22
        for status in ('applied','failed'):
            self.command['in_chain_status']=status
            self.command['failure_reasons_ids']=[1] if status=='failed' else None
            got=self.verify()
            self.assertEqual((got['status'],got['in_chain_status'],got['depth']),('pending',status,2))

    def test_failed_inclusion_does_not_mark_allocations_paid(self):
        self.command.update(in_chain_status='failed',failure_reasons_ids=[1])
        got=self.verify()
        self.assertEqual((got['status'],got['paid_nano'],got['fee_paid_nano']),('failed',0,1000000))

    def test_absent_and_other_fork_are_unknown_not_failed(self):
        self.commands=[]
        self.assertEqual(self.verify()['status'],'unknown')
        self.commands=[self.command];self.command['block_hash']='other'
        self.assertEqual(self.verify()['status'],'unknown')

    def test_duplicate_inclusions_on_best_chain_are_rejected(self):
        self.commands.append(copy.deepcopy(self.command))
        with self.assertRaises(batch_archive.BatchArchiveError): self.verify()

    def test_fork_row_does_not_hide_single_best_chain_inclusion(self):
        fork=copy.deepcopy(self.command);fork.update(block_hash='fork',chain_status='orphaned')
        self.commands.append(fork)
        self.assertEqual(self.verify()['status'],'applied')

    def test_body_payment_tampering_fails_closed(self):
        mutations=[('address','wrong'),('token','1'),('ordinality',3),('update_id',555),
            ('body.balance_change','4'),('body.balance_change','+1'),('body.balance_change','1.0'),
            ('body.call_depth',0),('body.increment_nonce',True),('body.authorization_kind','Proof'),
            ('body.implicit_account_creation_fee',True),('body.use_full_commitment',True),
            ('body.may_use_token','InheritFromParent'),('body.verification_key_hash_id',1),
            ('body.zkapp_valid_while_precondition_id',1),('call_data','1'),('events',[1]),('actions',[1]),
            ('changes.delegate_id',1),('app_state.element0',1),('account_state.element0',1),
            ('account_precondition.is_new',None),('account_precondition.nonce_id',3),
            ('network_precondition.blockchain_length_id',2),('staking_ledger.hash_id',2),
            ('next_epoch.epoch_seed','bad')]
        original=copy.deepcopy(self.updates)
        for path,value in mutations:
            with self.subTest(path=path,value=value):
                self.updates=copy.deepcopy(original); target=self.updates[1]
                parts=path.split('.')
                for part in parts[:-1]:target=target[part]
                target[parts[-1]]=value
                with self.assertRaises(batch_archive.BatchArchiveError):self.verify()

    def test_missing_extra_and_reordered_updates_rejected(self):
        original=copy.deepcopy(self.updates)
        for updates in (original[:2],original+[original[-1]],list(reversed(original))):
            self.updates=updates
            with self.assertRaises(batch_archive.BatchArchiveError):self.verify()

    def test_wrong_fee_payer_nonce_memo_fee_status_and_height_rejected(self):
        for field,value in [('wallet','other'),('nonce',8),('memo',memo('other')),('memo','batch_test'),('memo',memo('batch_test',padding=1)),('fee_nano','1000001'),
                            ('valid_until',44),('in_chain_status','oops'),('height',21),('era',4),
                            ('failure_reasons_ids',[1])]:
            with self.subTest(field=field):
                original=self.command[field];self.command[field]=value
                with self.assertRaises(batch_archive.BatchArchiveError):self.verify()
                self.command[field]=original

    def test_created_account_rejected(self):
        self.created=[{'account_identifier_id':11}]
        with self.assertRaises(batch_archive.BatchArchiveError):self.verify()

    def test_parent_ancestry_failure_propagates_and_closes_connection(self):
        with patch.object(preflight,'_connect',return_value=self.connection),\
             patch.object(preflight,'_rows',side_effect=self.rows),\
             patch.object(preflight,'check',side_effect=preflight.PreflightError('broken parent links')):
            with self.assertRaises(preflight.PreflightError):batch_archive.verify(CONFIG,EXPECTED)
        self.assertTrue(self.connection.closed)

    def test_wrong_network_or_wallet_never_queries_archive(self):
        for key in ('EXPECTED_CHAIN_ID','SEND_FROM_ADDRESS'):
            with patch.object(preflight,'_connect') as connect:
                with self.assertRaises(batch_archive.BatchArchiveError):batch_archive.verify(dict(CONFIG,**{key:'wrong'}),EXPECTED)
                connect.assert_not_called()

    def test_real_preflight_sql_proves_old_block_and_rejects_broken_parent(self):
        from tests.test_preflight import Archive, NODE, CONFIG as HEALTH_CONFIG
        for damage in (False,True):
            with self.subTest(broken_parent=damage):
                db=Archive()
                for height in range(26,801):
                    db.conn.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?)',
                        (height,'h'+str(height),height-1,'h'+str(height-1),height,3,
                         'canonical' if height<=795 else 'pending',height-1,'1'))
                db.conn.execute("UPDATE blocks SET chain_status='canonical' WHERE height<=795")
                if damage:db.conn.execute("UPDATE blocks SET parent_hash='wrong' WHERE height=600")
                node=copy.deepcopy(NODE)
                node['daemonStatus']['blockchainLength']='800'
                node['bestChain']=[{'stateHash':'h800','protocolState':{'consensusState':
                    {'blockchainLength':'800','epoch':'79'}}}]
                original_rows=preflight._rows
                def rows(connection,query,params=None):
                    return self.rows(connection,query,params) if connection is self.connection else original_rows(connection,query,params)
                with patch.object(preflight,'_connect',side_effect=[self.connection,db]),\
                     patch.object(preflight,'_graphql',return_value=node),\
                     patch.object(preflight,'_rows',side_effect=rows):
                    if damage:
                        with self.assertRaises(preflight.PreflightError):batch_archive.verify(HEALTH_CONFIG,EXPECTED)
                    else:
                        got=batch_archive.verify(HEALTH_CONFIG,EXPECTED)
                        self.assertEqual((got['status'],got['depth']),('applied',780))
                self.assertTrue(db.closed)

    def test_database_error_is_sanitized(self):
        with patch.object(preflight,'_connect',side_effect=RuntimeError('secret password')):
            with self.assertRaises(batch_archive.BatchArchiveError) as caught:batch_archive.verify(CONFIG,EXPECTED)
        self.assertNotIn('secret',str(caught.exception))

if __name__=='__main__': unittest.main()
