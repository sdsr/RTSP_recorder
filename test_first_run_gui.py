"""Real desktop first-run forms; only temporary settings and no camera requests."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

spec=importlib.util.spec_from_file_location('first_run_gui_test',Path(__file__).with_name('record.py'))
record=importlib.util.module_from_spec(spec);sys.modules[spec.name]=record;spec.loader.exec_module(record)


@unittest.skipUnless(os.environ.get('ED_TEST_DESKTOP_DISPLAY')=='1','requires an enabled Tk test display')
class FirstRunGuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk,self.ttk=tk,ttk
        self.temporary=tempfile.TemporaryDirectory(prefix='rtsp-first-gui-')
        self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'recording.json'
        self.data=json.loads(record.render_json_config())
        self.data['output_dir']=str(self.path.parent/'videos')
        self.app=None
        self.dialogs=mock.Mock();self.dialogs.askyesno.return_value=True
        self.addCleanup(self.cleanup)

    def open(self):
        self.path.write_text(json.dumps(self.data),encoding='utf-8')
        self.controller=record.RecorderUiController(self.path)
        self.root=self.tk.Tk();self.root.withdraw()
        self.errors=[];self.root.report_callback_exception=lambda *args:self.errors.append(args)
        self.app=record.RecorderDesktopApp(self.root,self.tk,self.ttk,(mock.Mock(),self.dialogs),self.controller)
        self.pump(lambda:self.app.loaded and not self.app.pending_action and bool(self.app.last_state))

    def pump(self,condition):
        deadline=time.monotonic()+5
        while not condition():
            if time.monotonic()>deadline:self.fail('Tk operation timed out: '+self.app.notice.get())
            self.root.update();time.sleep(.005)
        self.assertEqual(self.errors,[])

    def cleanup(self):
        if self.app is not None and not self.app.destroyed:
            self.app.request_close(force=True)
            self.pump(lambda:self.app.destroyed)
            self.app.worker.join(2)

    def test_first_run_has_blank_device_and_region_then_saves_only_entered_camera(self):
        self.open();app=self.app
        self.assertEqual(app.notebook.select(),str(app.device_tab.master.master))
        for key in ('camera_ips','camera_username','camera_password','sido','sigungu','eupmyeondong'):
            self.assertEqual(app.form[key].get(),'')
        self.assertEqual(app.camera_ips.get('1.0','end-1c'),'')
        self.assertTrue(app.start_button.instate(['disabled']))
        self.assertTrue(app.once_button.instate(['disabled']))
        app.form['intersection_name'].set('직접 입력한 현장');app.save()
        self.pump(lambda:not app.pending_action and not app.dirty)
        self.assertEqual(json.loads(self.path.read_text())['cameras'],[])
        app.camera_ips.insert('1.0','192.0.2.77')
        app.form['camera_username'].set('operator')
        app.form['camera_password'].set('TEST_ONLY')
        app.save();self.pump(lambda:not app.pending_action and not app.dirty and not app.last_state.get('setup_required'))
        saved=json.loads(self.path.read_text())
        parsed=record.urlsplit(saved['cameras'][0]['url'])
        self.assertEqual((parsed.hostname,parsed.username,parsed.password),('192.0.2.77','operator','TEST_ONLY'))
        self.assertEqual(saved['weather_condition']['region'],{'sido':'','sigungu':'','eupmyeondong':''})
        self.assertFalse(app.start_button.instate(['disabled']))
        self.assertFalse(self.dialogs.showerror.called)

    def test_existing_device_and_location_are_shown_without_rewriting_settings(self):
        self.data['cameras']=[{'name':'original','url':'rtsp://original_user:TEST_SAVED@192.0.2.88/live'}]
        self.data['weather_condition']['region']={'sido':'경상북도','sigungu':'구미시','eupmyeondong':''}
        self.open();original=self.path.read_bytes()
        self.assertEqual(self.app.form['camera_username'].get(),'original_user')
        self.assertEqual(self.app.form['camera_ips'].get(),'192.0.2.88')
        self.assertEqual(self.app.form['sido'].get(),'경상북도')
        self.assertEqual(self.app.form['sigungu'].get(),'구미시')
        self.assertEqual(self.app.form['camera_password'].get(),'')
        self.assertEqual(self.path.read_bytes(),original)
        self.assertFalse(self.app.last_state['setup_required'])


if __name__=='__main__':unittest.main()
