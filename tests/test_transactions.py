"""Interrupted transactions, undo/redo, and real PowerShell bridge checks."""
from argparse import Namespace
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'core'))
import executor
from safety import SafetyError


class TransactionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.organizer-test-',dir=PROJECT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'root'
        self.root.mkdir()
        self.env = patch.dict(os.environ,{'LOCALAPPDATA':str(self.base/'local'),
                                        'PYTHONIOENCODING':'utf-8'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.journal = executor.Journal(self.root)

    def put(self, name, content='synthetic'):
        p = self.root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(content,encoding='utf-8')
        return p

    def move(self, source, target, run='test-run'):
        with self.journal.locked():
            return executor.transaction(self.journal,run,'rename','move',str(source),str(target),'test')

    def undo(self,run):
        with contextlib.redirect_stdout(io.StringIO()):
            return executor.undo(Namespace(journal=str(self.journal.path),run_id=run,
                                            seq=0,last=False,list=False,dry_run=False))

    def recover(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return executor.recover(Namespace(root=str(self.root)))

    def test_prepare_failure_never_moves_user_file(self):
        source=self.put('before.txt')
        target=self.root/'after.txt'
        with patch.object(executor.Journal,'append',side_effect=OSError('journal unavailable')):
            with self.assertRaises(OSError):
                self.move(source,target)
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())
        self.assertTrue(self.journal.unresolved())
        self.recover()
        self.assertFalse(self.journal.unresolved())

    def test_interrupt_after_move_finishes_references_and_can_undo(self):
        source=self.put('A/file.txt').parent
        target=self.root/'B'
        probe=self.put('_organize/probe/node.json',json.dumps({'path':str(source),'action':'keep'}))
        rename=executor.rename_noreplace
        def crash(src,dst):
            rename(src,dst)
            raise OSError('simulated process interruption')
        with patch.object(executor,'rename_noreplace',side_effect=crash):
            with self.assertRaises(OSError):
                self.move(source,target)
        self.assertTrue(target.exists())
        self.assertTrue(self.journal.unresolved())
        self.recover()
        self.assertEqual(json.loads(probe.read_text())['path'],str(target))
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.undo('test-run'),0)
        self.assertTrue(source.exists())
        self.assertEqual(json.loads(probe.read_text())['path'],str(source))

    def test_partial_reference_update_is_resumed_idempotently(self):
        source=self.put('A/file.txt').parent
        target=self.root/'B'
        probes=[self.put(f'_organize/probe/node{i}.json',json.dumps({'path':str(source),'action':'keep'}))
                for i in range(2)]
        finish=executor.finish_references
        def crash(edits):
            finish(edits[:1])
            raise OSError('simulated interruption')
        with patch.object(executor,'finish_references',side_effect=crash):
            with self.assertRaises(OSError):
                self.move(source,target)
        self.recover()
        for probe in probes:
            self.assertEqual(json.loads(probe.read_text())['path'],str(target))
        self.assertEqual(self.recover(),0)

    def test_mirror_completion_restores_missing_main_suffix(self):
        source=self.put('before.txt')
        target=self.root/'after.txt'
        append=executor.Journal.append
        def crash(journal,*args,**kwargs):
            before=journal.path.read_bytes()
            result=append(journal,*args,**kwargs)
            status=kwargs.get('status',args[5] if len(args)>5 else 'done')
            if status=='done':
                journal.path.write_bytes(before)
                raise OSError('main log lost completion')
            return result
        with patch.object(executor.Journal,'append',new=crash):
            with self.assertRaises(OSError):
                self.move(source,target)
        self.assertTrue(target.exists())
        current_dir = self.journal.mirror_dir
        legacy_dir = self.base/'local'/'packtidy-mirror'/current_dir.name
        legacy_dir.parent.mkdir()
        current_dir.rename(legacy_dir)
        self.assertEqual(executor.Journal(self.root).mirror_dir, legacy_dir)
        self.recover()
        self.assertEqual(self.journal.mirror_dir, current_dir)
        self.assertFalse(legacy_dir.exists())
        self.assertEqual(self.journal.path.read_bytes(),self.journal.mirror.read_bytes())
        self.assertEqual(self.undo('test-run'),0)
        self.assertTrue(source.exists())

    def test_newer_mirrored_mkdir_identity_wins_over_stale_primary(self):
        target=self.root/'created'
        write=executor.atomic_write
        count=0
        def crash(path,data):
            nonlocal count
            if Path(path).parent==self.journal.tx_dir:
                count+=1
                if count==2:
                    raise OSError('primary manifest unavailable')
            write(path,data)
        with self.journal.locked():
            with patch.object(executor,'atomic_write',side_effect=crash):
                with self.assertRaises(OSError):
                    executor.transaction(self.journal,'mkdir-run','mkdir','mkdir','-',str(target),'test')
        self.assertFalse(target.exists())
        self.assertIsNotNone(self.journal.unresolved()[0]['identity'])
        self.recover()
        self.assertFalse(self.journal.unresolved())
        self.assertEqual(self.undo('mkdir-run'),0)
        self.assertFalse(target.exists())

    def test_mkdir_crash_before_identity_never_publishes_unknown_directory(self):
        target=self.root/'created'
        get_identity=executor.identity
        def crash(path):
            if 'staging' in Path(path).parts:
                raise OSError('interrupted immediately after staging mkdir')
            return get_identity(path)
        with self.journal.locked():
            with patch.object(executor,'identity',side_effect=crash):
                with self.assertRaises(OSError):
                    executor.transaction(self.journal,'mkdir-run','mkdir','mkdir','-',str(target),'test')
        self.assertFalse(target.exists())
        self.assertTrue(self.journal.unresolved())
        self.recover()
        self.assertFalse(self.journal.unresolved())

    def test_racing_destination_is_preserved_and_prepare_can_abort(self):
        source=self.put('before.txt','original')
        target=self.root/'after.txt'
        rename=executor.rename_noreplace
        def race(src,dst):
            Path(dst).write_text('racing destination')
            rename(src,dst)
        with patch.object(executor,'rename_noreplace',side_effect=race):
            with self.assertRaises(OSError):
                self.move(source,target)
        self.assertEqual(source.read_text(),'original')
        self.assertEqual(target.read_text(),'racing destination')
        self.recover()
        self.assertFalse(self.journal.unresolved())

    def test_new_marker_blocks_undo_without_leaking_path(self):
        source=self.put('before.txt')
        target=self.root/'after.txt'
        self.move(source,target)
        self.put('_organize/markers.tsv',f'path\tkind\tby\tnote\tts\n{target}\tSECRET\tuser\tx\tx\n')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=executor.undo(Namespace(journal=str(self.journal.path),run_id='test-run',
                                          seq=0,last=False,list=False,dry_run=False))
        self.assertEqual(result,1)
        self.assertTrue(target.exists())
        self.assertNotIn(str(target),output.getvalue())

    def test_replaced_target_is_never_undone(self):
        source=self.put('before.txt','original')
        target=self.root/'after.txt'
        self.move(source,target)
        target.unlink()
        target.write_text('replacement with a different size')
        self.assertEqual(self.undo('test-run'),1)
        self.assertEqual(target.read_text(),'replacement with a different size')

    def test_undo_redo_new_directories_and_move(self):
        source=self.put('source.txt')
        target=self.root/'one/two/source.txt'
        with self.journal.locked():
            executor.ensure_parents(self.journal,'batch',target.parent,'test')
            executor.transaction(self.journal,'batch','move','move',str(source),str(target),'test')
        self.assertEqual(self.undo('batch'),0)
        self.assertTrue(source.exists())
        self.assertFalse((self.root/'one').exists())
        undo_run=executor.read_journal(self.journal.path)[-1]['run_id']
        self.assertEqual(self.undo(undo_run),0)
        self.assertTrue(target.exists())
        self.assertEqual(self.undo('batch'),0)
        self.assertTrue(source.exists())
        self.assertFalse((self.root/'one').exists())

    def test_protected_destination_and_self_move_are_rejected(self):
        source=self.put('source/file.txt').parent
        for target in (self.root/'Windows',source/'nested'):
            prepared,blocked=executor.preflight(self.root,[dict(op='move',src=str(source),dst=str(target),reason='x')])
            self.assertFalse(prepared)
            self.assertTrue(blocked)
        self.assertTrue((source/'file.txt').exists())

    def test_policy_no_go_zones_accept_relative_and_absolute_and_reject_escape(self):
        source = self.put('allowed/file.txt').parent
        (self.root/'_organize').mkdir(exist_ok=True)
        policy_path = self.root/'_organize'/'policy.json'
        for zone in ('allowed', str(source)):
            policy_path.write_text(json.dumps({'no_go_zones': [zone]}), encoding='utf-8')
            prepared, blocked = executor.preflight(
                self.root, [dict(op='rename', src=str(source), dst='renamed', reason='test')])
            self.assertFalse(prepared)
            self.assertEqual(len(blocked), 1)
        policy_path.write_text(json.dumps({'no_go_zones': ['../outside']}), encoding='utf-8')
        with self.assertRaises(SafetyError):
            executor.preflight(self.root, [dict(op='rename', src=str(source), dst='renamed', reason='test')])
        policy_path.write_text(json.dumps({'no_go_zones': 'allowed'}), encoding='utf-8')
        with self.assertRaises(SafetyError):
            executor.preflight(self.root, [dict(op='rename', src=str(source), dst='renamed', reason='test')])

    def test_narrowing_the_root_keeps_parent_exclusions(self):
        private = self.put('shared/private/record.txt')
        public = self.put('shared/public/record.txt')
        self.put('_organize/policy.json', json.dumps({'no_go_zones': ['shared/private']}))
        action = dict(op='rename', src=str(private), dst='renamed.txt', reason='test')
        for scope in (self.root, private.parent.parent, private.parent):
            with self.subTest(scope=scope):
                prepared, blocked = executor.preflight(scope, [action])
                self.assertFalse(prepared)
                self.assertEqual(len(blocked), 1)
        prepared, blocked = executor.preflight(public.parent, [{**action, 'src': str(public)}])
        self.assertEqual(len(prepared), 1)
        self.assertFalse(blocked)
        self.assertTrue(private.exists())

    def test_apply_stops_on_missing_primary_markers_and_recover_restores_from_mirror(self):
        source = self.put('before.txt')
        markers = self.root/'_organize'/'markers.tsv'
        markers.parent.mkdir(parents=True, exist_ok=True)
        markers.write_text('path\tkind\tby\tnote\tts\n', encoding='utf-8')
        current_dir = self.journal.mirror_dir
        legacy_dir = self.base/'local'/'packtidy-mirror'/current_dir.name
        legacy_dir.mkdir(parents=True)
        legacy_dir.joinpath('markers.tsv').write_bytes(markers.read_bytes())
        markers.unlink()
        with self.assertRaises(SafetyError):
            self.journal.protection()
        self.assertFalse(current_dir.exists())
        actions = self.put('_organize/actions.tsv',
                           f'op\tsrc\tdst\treason\nrename\t{source}\tafter.txt\ttest\n')
        args = Namespace(root=str(self.root), actions=str(actions), rename=None, plan=None,
                         reason='-', batch_size=20, allow_cross_drive=False, dry_run=False)
        with self.assertRaises(SafetyError):
            executor.apply(args)
        self.assertTrue(source.exists())
        self.assertEqual(self.recover(), 0)
        self.assertTrue(markers.exists())
        self.assertEqual(markers.read_bytes(), self.journal.mirror_dir.joinpath('markers.tsv').read_bytes())

    def test_conflicting_mirror_directories_stop_writes(self):
        current_dir = self.journal.mirror_dir
        legacy_dir = self.base/'local'/'packtidy-mirror'/current_dir.name
        for directory in (current_dir, legacy_dir):
            directory.mkdir(parents=True)
        with self.assertRaisesRegex(SafetyError, '两套日志镜像'):
            with self.journal.locked():
                self.fail('A conflicting mirror must block use of the journal')
        self.assertFalse(self.journal.path.exists())
        self.assertTrue(legacy_dir.is_dir() and current_dir.is_dir())

    def test_rule_pack_adds_protection_without_removing_builtin_protection(self):
        source = self.put('custom-zone/file.txt').parent
        local = self.base/'local'/'packtidy'/'rules'
        local.mkdir(parents=True)
        package = {
            'version': 2, 'minPacktidy': '0.1.0',
            'protected': ['custom-zone'], 'categories': {'x': {'keys': ['x']}}
        }
        (local/'remote.json').write_text(json.dumps(package), encoding='utf-8')
        built_in_source = self.put('Windows/important.txt').parent
        prepared, blocked = executor.preflight(self.root, [
            dict(op='rename', src=str(source), dst='custom-renamed', reason='test'),
            dict(op='rename', src=str(built_in_source), dst='safe-renamed', reason='test'),
        ])
        self.assertFalse(prepared)
        self.assertEqual(len(blocked), 2)

    def test_parent_and_child_sources_preflight_as_one_ready_one_conflict(self):
        parent = self.put('A/child/file.txt').parents[1]
        child = parent/'child'
        destination = self.root/'B'
        destination.mkdir()
        prepared, blocked = executor.preflight(self.root, [
            dict(op='move', src=str(parent), dst=str(destination), reason='parent'),
            dict(op='move', src=str(child), dst=str(destination), reason='child'),
        ])
        self.assertEqual(len(prepared), 1)
        self.assertEqual(len(blocked), 1)
        self.assertTrue(parent.exists())

    def test_malformed_probe_fails_before_file_move(self):
        source=self.put('before.txt')
        self.put('_organize/probe/bad.json','{')
        prepared,blocked=executor.preflight(self.root,[dict(op='rename',src=str(source),dst='after.txt')])
        self.assertFalse(prepared)
        self.assertTrue(blocked)
        self.assertTrue(source.exists())

    def test_journal_mirrors_are_separated_by_root(self):
        other=self.base/'other'
        other.mkdir()
        self.assertNotEqual(self.journal.mirror,executor.Journal(other).mirror)

    def test_protected_root_is_rejected_before_creating_state(self):
        root=self.base/'Windows'
        root.mkdir()
        with self.assertRaises(SafetyError):
            executor.Journal(root)
        self.assertFalse((root/'_organize').exists())

    def test_missing_input_or_recovery_root_does_not_create_state(self):
        args=Namespace(root=str(self.root),actions=str(self.base/'missing.tsv'),
                       rename=None,plan=None,reason='-')
        with self.assertRaises(SafetyError):
            executor.read_actions(args)
        missing=self.base/'missing-root'
        with self.assertRaises(SafetyError):
            executor.recover(Namespace(root=str(missing)))
        self.assertFalse(missing.exists())
        self.assertFalse((self.root/'_organize').exists())

    @unittest.skipUnless(os.name=='nt','Windows exclusive file sharing')
    def test_real_locked_journal_stops_before_moving_file(self):
        shell=shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell unavailable')
        source=self.put('input.txt')
        destination=self.root/'dest'
        destination.mkdir()
        with self.journal.locked():
            pass
        actions=self.put('_organize/actions.tsv',
            f'op\tsrc\tdst\treason\nmove\t{source}\t{destination}\ttest\n')
        quote=lambda p: "'"+str(p).replace("'","''")+"'"
        script=self.base/'lock.ps1'
        script.write_text(
            f'$lock=[IO.File]::Open({quote(self.journal.path)},[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)\n'+
            f'try {{ & {quote(PROJECT/"scripts/apply.ps1")} -Root {quote(self.root)} -Actions {quote(actions)} }} finally {{ $lock.Dispose() }}\n',
            encoding='utf-8-sig')
        result=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(script)],
                              capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
        self.assertTrue(source.exists(),result.stdout+result.stderr)
        self.assertFalse((destination/source.name).exists())
        self.assertFalse(executor.read_journal(self.journal.path))

    def test_repeated_sequence_undo_after_redo_is_idempotent(self):
        source=self.put('A.txt')
        target=self.root/'B.txt'
        self.move(source,target)
        self.assertEqual(self.undo('test-run'),0)
        undo_run=executor.read_journal(self.journal.path)[-1]['run_id']
        self.assertEqual(self.undo(undo_run),0)
        boundary=executor.read_journal(self.journal.path)[-1]['seq']
        for unused in range(3):
            with contextlib.redirect_stdout(io.StringIO()):
                code=executor.undo(Namespace(journal=str(self.journal.path),run_id=None,
                                             seq=boundary,last=False,list=False,dry_run=False))
            self.assertEqual(code,0)
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())

    def test_exact_sequence_undo_then_exact_inverse_redo(self):
        first=self.put('first.txt')
        first_target=self.root/'first-moved.txt'
        second=self.put('second.txt')
        second_target=self.root/'second-moved.txt'
        self.move(first,first_target,'same-run')
        first_seq=executor.read_journal(self.journal.path)[-1]['seq']
        self.move(second,second_target,'same-run')
        second_seq=executor.read_journal(self.journal.path)[-1]['seq']
        result = subprocess.run([sys.executable, '-B', str(PROJECT / 'tidy_file_tree.py'), 'undo',
                                 '--journal', str(self.journal.path), '--exact-seq', str(second_seq),
                                 '--dry-run'], capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'[dry] undo seq {second_seq}:', result.stdout)
        self.assertNotIn(f'[dry] undo seq {first_seq}:', result.stdout)
        with contextlib.redirect_stdout(io.StringIO()):
            code=executor.undo(Namespace(journal=str(self.journal.path),run_id=None,last=False,
                                         seq=None,exact_seq=second_seq,list=False,dry_run=False))
        self.assertEqual(code,0)
        self.assertTrue(first_target.exists())
        self.assertTrue(second.exists())
        self.assertFalse(second_target.exists())
        undo_seq=executor.read_journal(self.journal.path)[-1]['seq']
        with contextlib.redirect_stdout(io.StringIO()):
            code=executor.undo(Namespace(journal=str(self.journal.path),run_id=None,last=False,
                                         seq=None,exact_seq=undo_seq,list=False,dry_run=False))
        self.assertEqual(code,0)
        self.assertTrue(first_target.exists())
        self.assertTrue(second_target.exists())

    def test_undo_preview_includes_directory_emptied_by_earlier_inverse(self):
        source = self.put('sample.txt')
        created = self.root / 'auto-created'
        target = created / source.name
        with self.journal.locked():
            executor.transaction(self.journal, 'batch', 'mkdir', 'mkdir', '-', str(created), 'test')
            executor.transaction(self.journal, 'batch', 'move', 'move', str(source), str(target), 'test')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = executor.undo(Namespace(journal=str(self.journal.path), run_id='batch', last=False,
                                           seq=None, list=False, dry_run=True))
        self.assertEqual(code, 0)
        self.assertIn(f'{created} -> -', output.getvalue())
        self.assertTrue(target.is_file())
        self.assertEqual(self.undo('batch'), 0)
        self.assertTrue(source.is_file())
        self.assertFalse(created.exists())

    def test_undo_move_preserves_replacement_swapped_before_transaction(self):
        source = self.put('source.txt', 'original')
        target = self.root / 'target.txt'
        self.move(source, target)
        transaction = executor.transaction

        def replace(*args, **kwargs):
            target.rename(self.root / 'held-original.txt')
            target.write_text('replacement', encoding='utf-8')
            return transaction(*args, **kwargs)

        with patch.object(executor, 'transaction', side_effect=replace):
            self.assertEqual(self.undo('test-run'), 1)
        self.assertEqual(target.read_text(encoding='utf-8'), 'replacement')
        self.assertEqual((self.root / 'held-original.txt').read_text(encoding='utf-8'), 'original')
        self.assertFalse(source.exists())

    def test_exact_sequence_rejects_unknown_sequence(self):
        source=self.put('item.txt')
        self.move(source,self.root/'moved.txt')
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SafetyError):
                executor.undo(Namespace(journal=str(self.journal.path),run_id=None,last=False,
                                        seq=None,exact_seq=999,list=False,dry_run=True))

    def test_pending_journal_without_manifest_stops_recovery_and_undo(self):
        source = self.put('recorded.txt')
        with self.journal.locked():
            self.journal.append('interrupted', 'move', str(source), str(self.root / 'other.txt'),
                                json.dumps({'tx': 'a' * 32, 'effect': 'move'}), 'pending')
        with self.assertRaisesRegex(SafetyError, '缺少可核对的恢复清单'):
            self.recover()
        with self.assertRaisesRegex(SafetyError, '缺少可核对的恢复清单'):
            self.undo('interrupted')
        self.assertTrue(source.is_file())

    def test_undo_mkdir_preserves_replacement_swapped_after_prepare(self):
        created=self.root/'created'
        with self.journal.locked():
            executor.transaction(self.journal,'mkdir-run','mkdir','mkdir','-',str(created),'test')
        seq=executor.read_journal(self.journal.path)[-1]['seq']
        append=self.journal.append
        swapped=False
        def swap_on_pending(run,op,before,after,reason,status='done'):
            nonlocal swapped
            result=append(run,op,before,after,reason,status)
            if status=='pending' and op=='undo-mkdir' and not swapped:
                created.rename(self.root/'original-created')
                created.mkdir()
                swapped=True
            return result
        with patch.object(executor.Journal,'append',side_effect=swap_on_pending):
            with contextlib.redirect_stdout(io.StringIO()):
                code=executor.undo(Namespace(journal=str(self.journal.path),run_id=None,last=False,
                                             seq=None,exact_seq=seq,list=False,dry_run=False))
        self.assertEqual(code,1)
        self.assertTrue(swapped)
        self.assertTrue(created.is_dir())
        self.assertTrue((self.root/'original-created').is_dir())

    def test_interrupted_secret_marker_update_blocks_apply_until_recovered(self):
        source=self.put('private.txt')
        write=executor.atomic_write
        def fail_primary(path,data):
            if Path(path)==self.root/'_organize/markers.tsv':
                raise OSError('primary marker write failed')
            return write(path,data)
        args=Namespace(root=str(self.root),action='add',path=str(source),
                       kind='SECRET',by='user',note='test')
        with patch.object(executor,'atomic_write',side_effect=fail_primary):
            with self.assertRaises(OSError):
                executor.marker_command(args)
        self.assertTrue(self.journal.pending_marker_update())
        with self.assertRaises(SafetyError):
            with self.journal.locked():
                pass
        self.recover()
        self.assertFalse(self.journal.pending_marker_update())
        prepared,blocked=executor.preflight(self.root,[dict(op='rename',src=str(source),dst='changed.txt')])
        self.assertFalse(prepared)
        self.assertTrue(blocked)
        self.assertTrue(source.exists())

    def test_note_does_not_reveal_secret_in_marker_listing(self):
        source=self.put('secret.txt')
        self.put('_organize/markers.tsv', 'path\tkind\tby\tnote\tts\n'+
                 f'{source}\tSECRET\tuser\ttest\tx\n{source}\tNOTE\tuser\tcomment\tx\n')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            executor.marker_command(Namespace(root=str(self.root),action='list'))
        self.assertNotIn(str(source),output.getvalue())

    def test_reference_migration_updates_marker_mirror(self):
        source=self.put('A/file.txt').parent
        target=self.root/'B'
        self.put('_organize/markers.tsv',f'path\tkind\tby\tnote\tts\n{source}\tNOTE\tuser\ttest\tx\n')
        self.move(source,target)
        self.assertEqual((self.root/'_organize/markers.tsv').read_bytes(),
                         (self.journal.mirror_dir/'markers.tsv').read_bytes())
        self.assertEqual(self.undo('test-run'),0)
        self.assertEqual((self.root/'_organize/markers.tsv').read_bytes(),
                         (self.journal.mirror_dir/'markers.tsv').read_bytes())

    def test_real_powershell_bridges(self):
        shells=[]
        for name in ('pwsh','powershell'):
            path=shutil.which(name)
            if path and path not in shells:
                shells.append(path)
        if not shells:
            self.skipTest('PowerShell unavailable')
        for index,shell in enumerate(shells):
            with self.subTest(shell=shell):
                root=self.root/str(index)
                root.mkdir()
                source=root/'输入[1].txt'
                source.write_text('hello')
                actions=self.base/f'actions{index}.tsv'
                actions.write_text(f'op\tsrc\tdst\treason\nrename\t{source}\t输出[1].txt\ttest\n',encoding='utf-8')
                def invoke(script,*args):
                    return subprocess.run([shell,'-NoProfile','-NonInteractive','-File',
                        str(PROJECT/'scripts'/script),*map(str,args)],capture_output=True,text=True,
                        encoding='utf-8',errors='replace',timeout=30)
                r=invoke('apply.ps1','-Root',root,'-Actions',actions)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertTrue((root/'输出[1].txt').exists())
                rid=executor.read_journal(root/'_organize/journal.tsv')[0]['run_id']
                r=invoke('undo.ps1','-Journal',root/'_organize/journal.tsv','-RunId',rid)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertTrue(source.exists())
                r=invoke('markers.ps1','-Root',root,'-Action','add','-Path',source,'-Kind','SECRET')
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertNotIn(str(source),r.stdout)
                r=invoke('markers.ps1','-Root',root,'-Action','check','-Path',source)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertIn('SECRET',r.stdout)
                r=invoke('recover.ps1','-Root',root)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)
                helper=self.base/f'journal-helper{index}.ps1'
                quote=lambda p: "'"+str(p).replace("'","''")+"'"
                helper.write_text(
                    f'. {quote(PROJECT/"scripts/journal.ps1")}\n'+
                    f'$path={quote(root/"_organize/journal.tsv")}\n'+
                    '$null=Add-Journal -Path $path -RunId helper -Op note -Reason "helper test"\n'+
                    '$rows=@(Read-Journal -Path $path)\n'+
                    'if (@($rows | Where-Object { $_.op -eq "note" }).Count -ne 1) { throw "read mismatch" }\n'+
                    '$runs=@(Get-JournalRuns -Path $path)\n'+
                    'if (-not ($runs | Where-Object { $_.run_id -eq "helper" })) { throw "run missing" }\n',
                    encoding='utf-8-sig')
                r=subprocess.run([shell,'-NoProfile','-NonInteractive','-File',str(helper)],
                                  capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
                self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_system_root_and_nested_deny_cannot_be_bypassed(self):
        from safety import is_system_root, nested_verdict
        if os.name == 'nt':
            sys_drive = os.environ.get('SystemDrive', 'C:')
            self.assertTrue(is_system_root(sys_drive + '\\'))
            self.assertTrue(is_system_root(sys_drive + '\\Users'))
            self.assertFalse(is_system_root(self.root))
            with self.assertRaises(SafetyError):
                executor.Journal(sys_drive + '\\')
            if os.path.isdir(sys_drive + '\\Users'):
                with self.assertRaises(SafetyError):
                    executor.Journal(sys_drive + '\\Users')
        for blocked in (
            self.root / 'Windows' / 'Temp' / 'a.tmp',
            self.root / 'AppData' / 'Local' / 'Temp' / 'b.tmp',
            self.root / 'Program Files' / 'App' / 'cache' / 'c.dat',
            self.root / '.ssh' / 'id_ed25519',
            self.root / 'Recovery' / 'OEM' / 'img.wim',
            self.root / 'Downloads' / 'proj' / 'node_modules' / 'pkg' / 'cache',
        ):
            allowed, _ = nested_verdict(blocked)
            self.assertFalse(allowed, f'should block {blocked}')
        # Ordinary PROTECTED_NAMES (e.g. Games) still allow nested Screenshots/Exports
        allowed, _ = nested_verdict(self.root / 'Games' / 'MyGame' / 'Screenshots' / 'shot.png')
        self.assertTrue(allowed)

    def test_system_drive_opt_in_external_state_and_undo_resolution(self):
        if os.name != 'nt':
            self.skipTest('Windows-specific system drive policy')
        from unittest.mock import patch
        import history
        import safety
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        fake_local = Path(self.temp.name) / 'fake-localappdata'
        fake_local.mkdir()
        with patch.dict(os.environ, {'LOCALAPPDATA': str(fake_local)}):
            journal = executor.Journal(sys_drive, allow_system_drive=True)
            self.assertTrue(safety.within(journal.state_dir, fake_local, inclusive=False))
            self.assertFalse(safety.within(journal.state_dir, Path(sys_drive) / '_organize'))
            with journal.locked():
                pass
            self.assertTrue((journal.state_dir / 'scope.json').is_file())
            self.assertTrue(journal.path.is_file())
            resolved_root, is_sys = executor.resolve_journal_root(journal.path)
            self.assertTrue(is_sys)
            self.assertEqual(safety.key(resolved_root), safety.key(sys_drive))
            # Undo rejects system-drive journal unless --allow-system-drive is explicit
            self.assertNotEqual(executor.main(['undo', '--journal', str(journal.path), '--list']), 0)
            self.assertEqual(
                executor.main(['undo', '--journal', str(journal.path), '--list', '--allow-system-drive']),
                0,
            )
            # Forged scope.json outside the canonical system-state dir is rejected
            forged = self.root / 'forged'
            forged.mkdir()
            (forged / 'scope.json').write_text(json.dumps({'root': sys_drive}), encoding='utf-8')
            (forged / 'journal.tsv').write_bytes(safety.encode_tsv([], executor.FIELDS))
            with self.assertRaises(SafetyError):
                executor.resolve_journal_root(forged / 'journal.tsv')
            # History payload exposes the actual external journal path and systemDrive flag
            hist = history.collect_history(journal, journal.protection())
            self.assertEqual(hist['journalPath'], str(journal.path))
            self.assertTrue(hist['systemDrive'])

    def test_skeleton_paths_dotfiles_and_system_attr_protection(self):
        if os.name != 'nt':
            self.skipTest('Windows-specific system drive policy')
        import ctypes
        from safety import is_skeleton_path, is_system_attr, nested_verdict
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        self.assertTrue(is_skeleton_path(sys_drive))
        self.assertTrue(is_skeleton_path(sys_drive + 'Users'))
        self.assertTrue(is_skeleton_path(sys_drive + 'Users\\alice'))
        self.assertTrue(is_skeleton_path(sys_drive + 'Users\\alice\\Downloads'))
        self.assertTrue(is_skeleton_path(sys_drive + 'Users\\alice\\Documents'))
        self.assertFalse(is_skeleton_path(sys_drive + 'Users\\alice\\Downloads\\book.pdf'))
        self.assertFalse(is_skeleton_path(sys_drive + 'MyArchive\\note.txt'))
        for blocked in (
            sys_drive + 'Drivers\\gpu.inf',
            sys_drive + 'Boot\\BCD',
            sys_drive + '.GamingRoot',
            sys_drive + 'Users\\alice\\.vscode\\settings.json',
            sys_drive + 'Users\\alice\\.gitconfig',
            sys_drive + 'Users\\alice\\NTUSER.DAT',
            sys_drive + 'Users\\alice\\ntuser.dat.LOG1',
        ):
            allowed, _ = nested_verdict(blocked)
            self.assertFalse(allowed, f'expected blocked: {blocked}')
        # Drivers on a non-system drive is NOT falsely blocked
        other_drive = 'Z:\\' if not sys_drive.upper().startswith('Z:') else 'Y:\\'
        self.assertTrue(nested_verdict(other_drive + 'Software\\Drivers\\gpu.inf')[0])

        # guard() blocks skeleton paths as movable targets, but allows user shell folders as containers
        protection = executor.Protection([], set())
        with self.assertRaises(SafetyError):
            executor.guard(sys_drive + 'Users\\alice\\Downloads', sys_drive, protection)
        executor.guard(sys_drive + 'Users\\alice\\Documents', sys_drive, protection, allow_container=True)

        # Windows FILE_ATTRIBUTE_SYSTEM protection
        sys_dir = self.root / 'HasSysFile'
        sys_dir.mkdir()
        sys_file = sys_dir / 'critical.sys'
        sys_file.write_text('x', encoding='utf-8')
        ini_dir = self.root / 'NormalAlbum'
        ini_dir.mkdir()
        ini_file = ini_dir / 'desktop.ini'
        ini_file.write_text('[.ShellClassInfo]\n', encoding='utf-8')
        set_attr = ctypes.windll.kernel32.SetFileAttributesW
        try:
            self.assertNotEqual(set_attr(str(sys_file), 0x4), 0)
            self.assertNotEqual(set_attr(str(ini_file), 0x6), 0)
            self.assertTrue(is_system_attr(sys_file))
            self.assertTrue(is_system_attr(ini_file))
            with self.assertRaises(SafetyError):
                executor.guard(sys_file, self.root, protection)
            with self.assertRaises(SafetyError):
                executor.guard(sys_dir, self.root, protection, descendants=True)
            with self.assertRaises(SafetyError):
                executor.guard(ini_file, self.root, protection)
            # Moving a normal user folder containing Windows' desktop.ini is allowed
            executor.guard(ini_dir, self.root, protection, descendants=True)
        finally:
            set_attr(str(sys_file), 0x80)
            set_attr(str(ini_file), 0x80)

    def test_env_dependency_and_toolchain_rename_move_protection(self):
        from unittest.mock import patch
        # 1. Directory referenced in PATH / toolchain env var cannot be moved OR renamed
        sdk_parent = self.root / 'MySDK'
        sdk_bin = sdk_parent / 'tools' / 'bin'
        sdk_bin.mkdir(parents=True)
        normal_file = self.root / 'note.txt'
        normal_file.write_text('ok', encoding='utf-8')
        protection = executor.Protection([], set())
        with patch.dict(os.environ, {'CUSTOM_SDK_BIN': str(sdk_bin)}):
            # Moving or renaming the referenced dir OR its ancestor is blocked
            with self.assertRaises(SafetyError):
                executor.guard(sdk_parent, self.root, protection, descendants=True)
            with self.assertRaises(SafetyError):
                executor.guard(sdk_bin, self.root, protection, descendants=True)
            # Browsing sdk_parent as container is allowed, while sdk_bin itself is blocked
            executor.guard(sdk_parent, self.root, protection, allow_container=True)
            with self.assertRaises(SafetyError):
                executor.guard(sdk_bin, self.root, protection, allow_container=True)
            # Sibling normal file is completely unaffected
            executor.guard(normal_file, self.root, protection)
            # preflight blocks rename on sdk_parent
            prepared, blocked = executor.preflight(
                str(self.root),
                [{'op': 'rename', 'src': str(sdk_parent), 'dst': 'RenamedSDK', 'reason': 'test'}],
            )
            self.assertEqual(prepared, [])
            self.assertEqual(len(blocked), 1)
            self.assertIn('环境变量', blocked[0])

        # 2. Shallow toolchain markers (pyvenv.cfg, conda-meta, .vcpkg-root, flutter) block move/rename
        venv_dir = self.root / 'myenv'
        venv_dir.mkdir()
        (venv_dir / 'pyvenv.cfg').write_text('home = /usr/bin\n', encoding='utf-8')
        vcpkg_dir = self.root / 'vcpkg_local'
        vcpkg_dir.mkdir()
        (vcpkg_dir / '.vcpkg-root').write_text('', encoding='utf-8')
        for protected_dir in (venv_dir, vcpkg_dir):
            with self.assertRaises(SafetyError):
                executor.guard(protected_dir, self.root, protection, descendants=True)
            prepared, blocked = executor.preflight(
                str(self.root),
                [{'op': 'rename', 'src': str(protected_dir), 'dst': 'OtherName', 'reason': 'test'}],
            )
            self.assertEqual(prepared, [])
            self.assertEqual(len(blocked), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows AppData and user profile paths')
    def test_appdata_and_dev_cache_cleanup_mode_is_narrowly_scoped(self):
        from safety import nested_verdict
        sys_drive = os.environ.get('SystemDrive', 'C:').rstrip('\\/') + '\\'
        safe_caches = (
            sys_drive + 'Users\\alice\\AppData\\Local\\Temp\\installer.tmp',
            sys_drive + 'Users\\alice\\AppData\\Local\\CrashDumps\\app.dmp',
            sys_drive + 'Users\\alice\\AppData\\Local\\pip\\cache\\pkg.whl',
            sys_drive + 'Users\\alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache\\f_001',
            sys_drive + 'Users\\alice\\AppData\\Local\\Mozilla\\Firefox\\Profiles\\sample.default-release\\cache2\\entries\\entry',
            sys_drive + 'Users\\alice\\.cache\\huggingface\\hub\\blob',
            sys_drive + 'Users\\alice\\.gradle\\caches\\modules-2\\a.jar',
        )
        always_blocked = (
            sys_drive + 'Users\\alice\\AppData',
            sys_drive + 'Users\\alice\\AppData\\Local',
            sys_drive + 'Users\\alice\\AppData\\Local\\Programs\\VSCode\\Code.exe',
            sys_drive + 'Users\\alice\\AppData\\Local\\Packages\\App\\LocalState\\db',
            sys_drive + 'Users\\alice\\AppData\\Local\\Microsoft\\Credentials\\cred',
            sys_drive + 'Users\\alice\\AppData\\Roaming\\Microsoft\\Protect\\key',
            sys_drive + 'Users\\alice\\AppData\\Local\\tidy-file-tree-mirror\\abc\\journal.tsv',
            sys_drive + 'Users\\alice\\AppData\\Local\\Microsoft\\Windows\\UsrClass.dat',
            sys_drive + 'Users\\alice\\AppData\\Roaming\\MyApp\\settings.json',
            sys_drive + 'Users\\alice\\.ssh\\id_ed25519',
            sys_drive + 'Users\\alice\\.vscode\\settings.json',
            sys_drive + 'Windows\\Temp\\a.tmp',
            sys_drive + 'Users\\alice\\AppData\\Local\\UnknownApp\\Cache\\save.dat',
            sys_drive + 'Users\\alice\\AppData\\Local\\UnknownApp\\Caches\\save.dat',
            sys_drive + 'Users\\alice\\AppData\\Local\\SomeApp\\cache_data\\save.dat',
        )
        # Default organize mode: safe caches are still blocked
        for p in safe_caches:
            self.assertFalse(nested_verdict(p)[0], f'default mode should block {p}')
        # Cleanup mode (allow_cache_cleanup=True): safe caches allowed, dangerous areas stay locked
        for p in safe_caches:
            self.assertTrue(nested_verdict(p, allow_cache_cleanup=True)[0], f'cleanup mode should allow {p}')
        for p in always_blocked:
            self.assertFalse(nested_verdict(p, allow_cache_cleanup=True)[0], f'cleanup mode MUST still block {p}')
        # Container browsing in cleanup mode allows traversing AppData\Local to reach Temp
        self.assertTrue(nested_verdict(sys_drive + 'Users\\alice\\AppData\\Local',
                                       allow_cache_cleanup=True, allow_container=True)[0])
        self.assertFalse(nested_verdict(sys_drive + 'Users\\alice\\AppData\\Local\\Programs',
                                        allow_cache_cleanup=True, allow_container=True)[0])
        profile = sys_drive + 'Users\\alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default'
        self.assertFalse(nested_verdict(profile, allow_cache_cleanup=True)[0])
        self.assertTrue(nested_verdict(profile, allow_cache_cleanup=True, allow_container=True)[0])


if __name__=='__main__':
    unittest.main(verbosity=2)
