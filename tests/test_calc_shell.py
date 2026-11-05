import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class CalcShellTests(unittest.TestCase):
    def invoke(self, args, *, fail_tunnel=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            shutil.copy(Path(__file__).resolve().parents[1]/'calc.sh',root/'calc.sh')
            for name in ('tunnel','payout_cli','calc_rewards'):
                source="import json,sys\nfrom pathlib import Path\np=Path('events')\nwith p.open('a') as f: f.write(json.dumps([%r,sys.argv[1:]])+'\\n')\n"%name
                if name=='tunnel' and fail_tunnel: source+='raise SystemExit(1)\n'
                if name=='payout_cli': source+="print('8')\n"
                (root/(name+'.py')).write_text(source)
            result=subprocess.run(['bash',str(root/'calc.sh')]+args,cwd='/',capture_output=True,text=True)
            events=[json.loads(x) for x in (root/'events').read_text().splitlines()] if (root/'events').exists() else []
            return result,events
    def test_explicit_epoch_still_ensures_tunnel_and_preflight_before_calculator(self):
        for args,expected in [(['--epoch','6','--vrf'],['--epoch','6','--vrf']),(['6','--minimum','0.5'],['--epoch','6','--minimum','0.5']),(['--epoch=6','--memo','a b'],['--epoch=6','--memo','a b'])]:
            with self.subTest(args=args):
                result,events=self.invoke(args)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual([e[0] for e in events],['tunnel','payout_cli','calc_rewards'])
                self.assertEqual(events[-1][1],expected)
                self.assertIn('6',events[1][1])
                self.assertIn('Туннель: OK',result.stderr)
                self.assertIn('Проверка валидатора и архива',result.stderr)
    def test_current_epoch_comes_from_health_snapshot(self):
        result,events=self.invoke(['--vrf'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(events[-1],['calc_rewards',['--epoch','8','--vrf']])
    def test_sourced_aliases_forward_existing_balance_and_doctor_status(self):
        source=Path(__file__).resolve().parents[1]/'scripts'/'aliases.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'scripts').mkdir(); shutil.copy(source,root/'scripts'/'aliases.sh')
            (root/'calc.sh').write_text('#!/bin/sh\nprintf "calc:%s\\n" "$*"\n')
            (root/'calc.sh').chmod(0o700)
            (root/'payout_cli.py').write_text('import sys; print("cli:"+" ".join(sys.argv[1:]))')
            for shell in ('bash','zsh'):
                executable=shutil.which(shell)
                if not executable: continue
                script='. "$1"; balance_mina() { mina_calc "$@"; }; balance_mina --epoch 6; mina_doctor --epoch 6; mina_status'
                result=subprocess.run([executable,'-c',script,'test',str(root/'scripts'/'aliases.sh')],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.splitlines(),['calc:--epoch 6','cli:doctor --epoch 6','cli:status'])
    def test_help_needs_no_network(self):
        result,events=self.invoke(['--help'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(events,[['calc_rewards',['--help']]])
    def test_tunnel_failure_prevents_any_database_calculation(self):
        result,events=self.invoke(['6'],fail_tunnel=True)
        self.assertNotEqual(result.returncode,0); self.assertEqual([e[0] for e in events],['tunnel'])

if __name__=='__main__': unittest.main()
