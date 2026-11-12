import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import shutil
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import autoprinter as ap
import config_profiles as cp
import config_sync as cli
from test_safety import Guard


class ConfigProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.config=self.root/'printer_data/config'
        self.config.mkdir(parents=True)
        self.manual()

    def manual(self,pin='PG14'):
        (self.config/'printer.cfg').write_text('[include macros500.cfg]\n[include settings500.cfg]\n[stepper_z]\nendstop_pin: '+pin+'\n')

    def cartographer(self):
        (self.config/'printer.cfg').write_text('[include macros500.cfg]\n[include settings500.cfg]\n[include cartographer.cfg]\n[stepper_z]\nendstop_pin: probe:z_virtual_endstop\n')
        (self.config/'cartographer.cfg').write_text('[cartographer]\nmcu: cartographer\n[mcu cartographer]\nserial: /dev/serial/by-id/usb-Cartographer-example\n')

    def install_version(self,name,ver,extra=''):
        (self.config/name).write_text('## Macro 500P ##\n## Version: '+ver+' ##\n## Date ##\n'+extra)

    def apply(self,plan,guard=None,write=None,hook=None):
        guard=guard or Guard()
        def default_write(path,data,mode):
            guard.check(); Path(path).write_bytes(data.encode() if isinstance(data,str) else data)
        return cp.apply(self.config,plan,self.root/'before',guard,write or default_write,before_write=hook or (lambda:None))

    def test_bundle_has_exact_expected_versions_and_hashes(self):
        for profile,ver in [('cartographer','6.5'),('manual-z','5.6')]:
            for spec in cp.templates(profile).values(): self.assertEqual(cp.version(spec['data']),ver)

    def test_first_three_lines_only_bom_and_crlf_supported(self):
        self.assertEqual(cp.version(b'\xef\xbb\xbf## File\r\n## Version: 6.5 ##\r\n## date\r\n'),'6.5')
        with self.assertRaises(cp.ConfigError): cp.version('# one\n# two\n# three\n## Version: 6.5 ##\n')

    def test_malformed_or_ambiguous_version_refuses(self):
        for data in ['# Version: beta\n','# Version: 6.5\n# Version: 6.6\n','']:
            with self.assertRaises(cp.ConfigError): cp.version(data)

    def test_numeric_not_float_or_mtime_comparison(self):
        self.assertGreater(cp.compare('6.10','6.9'),0)
        self.assertEqual(cp.compare('6.5.0','6.5'),0)

    def test_tampered_bundle_is_rejected(self):
        assets=self.root/'assets'; shutil.copytree(cp.ROOT,assets)
        with (assets/'manual-z/macros500.cfg').open('ab') as stream: stream.write(b'changed')
        with self.assertRaises(cp.ConfigError): cp.templates('manual-z',assets)

    def test_manual_config_overrides_spare_cartographer_usb(self):
        self.assertEqual(cp.plan(self.config,current_cartographer=True)['profile'],'manual-z')

    def test_cartographer_config_survives_probe_unplug(self):
        self.cartographer()
        self.assertEqual(cp.plan(self.config,current_cartographer=False)['profile'],'cartographer')

    def test_other_probe_sensorless_and_conflict_refuse(self):
        for pin in ['probe:z_virtual_endstop','tmc5160_stepper_z:virtual_endstop']:
            self.manual(pin)
            with self.assertRaises(cp.ConfigError): cp.plan(self.config)
        self.cartographer(); self.manual('PG14')
        with (self.config/'printer.cfg').open('a') as stream: stream.write('[include cartographer.cfg]\n')
        with self.assertRaises(cp.ConfigError): cp.plan(self.config)

    def test_explicit_profile_cannot_override_contradicting_printer(self):
        with self.assertRaises(cp.ConfigError): cp.plan(self.config,'cartographer')

    def test_fresh_requires_choice_unless_named_probe_currently_detected(self):
        (self.config/'printer.cfg').unlink()
        with self.assertRaises(cp.ConfigError): cp.plan(self.config)
        self.assertEqual(cp.plan(self.config,'manual-z')['profile'],'manual-z')
        self.assertEqual(cp.plan(self.config,current_cartographer=True)['profile'],'cartographer')

    def test_include_cycle_and_external_include_refuse(self):
        (self.config/'printer.cfg').write_text('[include printer.cfg]\n')
        with self.assertRaises(cp.ConfigError): cp.plan(self.config)
        (self.root/'outside.cfg').write_text('[stepper_z]\nendstop_pin: PG14\n')
        (self.config/'printer.cfg').write_text('[include ../../outside.cfg]\n')
        with self.assertRaises(cp.ConfigError): cp.plan(self.config)

    def test_missing_managed_includes_can_be_repaired(self):
        planned=cp.plan(self.config)
        self.assertTrue(planned['changes_required'])
        self.assertEqual({x['action'] for x in planned['files'].values()},{'install-missing'})

    def test_older_files_replaced_and_backed_up_exactly(self):
        for name in cp.NAMES: self.install_version(name,'5.5','local prior content\n')
        original={name:(self.config/name).read_bytes() for name in cp.NAMES}
        printer=(self.config/'printer.cfg').read_bytes()
        hook=Mock(); changed=self.apply(cp.plan(self.config),hook=hook)
        self.assertEqual(set(changed),set(cp.NAMES))
        for name in cp.NAMES:
            self.assertEqual((self.root/'before'/name).read_bytes(),original[name])
            self.assertEqual((self.config/name).read_bytes(),cp.templates('manual-z')[name]['data'])
        self.assertEqual((self.config/'printer.cfg').read_bytes(),printer)
        self.assertEqual(hook.call_count,3)
        journal=json.loads((self.root/'before/transaction.json').read_text())
        self.assertEqual(journal['status'],'CONFIG_FILES_UPDATED_REVIEW_REQUIRED')

    def test_same_version_edits_and_newer_are_preserved(self):
        self.install_version(cp.NAMES[0],'5.6','customizations\n')
        self.install_version(cp.NAMES[1],'5.7','newer\n')
        planned=cp.plan(self.config)
        self.assertEqual([v['action'] for v in planned['files'].values()],['keep-same-version-edits','keep-newer'])
        self.assertEqual(self.apply(planned),[])
        self.assertFalse((self.root/'before').exists())

    def test_identical_is_noop_and_hashes_match_after_missing_install(self):
        self.apply(cp.plan(self.config))
        planned=cp.plan(self.config)
        self.assertFalse(planned['changes_required'])
        self.assertTrue(all(v['action']=='keep-identical' for v in planned['files'].values()))

    def test_unversioned_blocks_pair_before_missing_partner_written(self):
        (self.config/cp.NAMES[0]).write_text('unknown header\n')
        planned=cp.plan(self.config)
        with self.assertRaises(cp.ConfigError): self.apply(planned)
        self.assertFalse((self.config/cp.NAMES[1]).exists())
        self.assertFalse((self.root/'before').exists())

    def test_changed_since_plan_refuses_before_backup_or_writes(self):
        planned=cp.plan(self.config)
        self.install_version(cp.NAMES[0],'9.0')
        with self.assertRaises(cp.ConfigError): self.apply(planned)
        self.assertFalse((self.root/'before').exists())

    def test_cancellation_after_first_write_retains_both_originals(self):
        for name in cp.NAMES: self.install_version(name,'5.5')
        original={name:(self.config/name).read_bytes() for name in cp.NAMES}
        guard=Guard()
        def write(path,data,mode):
            guard.check(); path.write_bytes(data.encode() if isinstance(data,str) else data)
            if path==self.config/cp.NAMES[0]: guard.cancelled.set()
        with self.assertRaises(ap.Removed): self.apply(cp.plan(self.config),guard,write)
        self.assertEqual((self.config/cp.NAMES[1]).read_bytes(),original[cp.NAMES[1]])
        for name in cp.NAMES: self.assertEqual((self.root/'before'/name).read_bytes(),original[name])

    def test_inactive_gate_failure_writes_nothing(self):
        def heating(): raise ap.Refuse('not inactive')
        with self.assertRaises(ap.Refuse): self.apply(cp.plan(self.config),hook=heating)
        self.assertFalse((self.root/'before').exists())

    @unittest.skipIf(sys.platform=='win32','POSIX symlink creation')
    def test_managed_symlink_refuses(self):
        (self.config/cp.NAMES[0]).symlink_to(self.root/'elsewhere.cfg')
        with self.assertRaises(cp.ConfigError): cp.plan(self.config)

    def test_config_cli_cannot_flash_or_change_network(self):
        for flag in ['--flash','--enable-wifi','--enable-flashing-capability']:
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit): cli.arguments([flag])

    def test_full_stack_never_restarts_after_config_changes_even_with_prior_confirmations(self):
        inst=ap.Installer(ap.arguments(['--apply','--maintenance-confirmed','--display-confirmed','--machine-config-confirmed']),{},Guard())
        for name in ['validate_layout','validate_manifest','ethernet_check','idle_check','setup_state','stop_stack','backup','install_packages','configure_wifi','trim_services']:
            setattr(inst,name,Mock())
        inst.guard.cancelled.wait=Mock()
        inst.stage_stack=Mock(return_value={})
        inst.activate_stack=lambda paths:inst.report.update(config_changes=['macros500.cfg'])
        inst.command=Mock(); inst.verify_ready=Mock()
        with patch.object(ap.os,'geteuid',return_value=0,create=True),patch.object(ap,'supported_host'),patch.object(ap,'check_free'),contextlib.redirect_stdout(io.StringIO()): inst.apply({})
        self.assertEqual(inst.report['status'],'INSTALLED_NEEDS_VALIDATION')
        inst.command.assert_not_called(); inst.verify_ready.assert_not_called()


if __name__=='__main__': unittest.main()
