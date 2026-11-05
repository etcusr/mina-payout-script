import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import payout_cli


class CliTests(unittest.TestCase):
    def test_doctor_print_epoch_uses_fresh_preflight_and_tunnel(self):
        events=[]
        def ensure(config): events.append('ensure'); return {}
        def check(config, **kwargs):
            events.append(('check', kwargs)); return {'ok':True,'current_epoch':8}
        output=io.StringIO()
        with patch.object(payout_cli, 'load_config', return_value={}), patch.object(payout_cli.tunnel,'ensure',side_effect=ensure), patch.object(payout_cli.preflight,'check',side_effect=check), contextlib.redirect_stdout(output):
            code=payout_cli.main(['doctor','--epoch','6','--print-epoch'])
        self.assertEqual(code,0); self.assertEqual(output.getvalue(),'8\n')
        self.assertEqual(events,['ensure',('check',{'epoch':6,'require_wallet':False})])
    def test_doctor_keeps_sanitized_actionable_error(self):
        error=io.StringIO()
        with patch.object(payout_cli,'load_config',return_value={}), patch.object(payout_cli.tunnel,'ensure',return_value={}), contextlib.redirect_stderr(error):
            code=payout_cli.main(['doctor'])
        self.assertEqual(code,1); self.assertIn('EXPECTED_CHAIN_ID',error.getvalue())
    def test_status_never_initializes_missing_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'not-created'; output=io.StringIO()
            with contextlib.redirect_stdout(output): code=payout_cli.main(['status','--state-dir',str(root)])
            self.assertEqual(code,0); self.assertFalse(root.exists())
            self.assertEqual(json.loads(output.getvalue())['journal'],'not_initialized')

if __name__=='__main__': unittest.main()
