"""Real CPU image-processing checks; Pillow is optional for the web application."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
import ai_runner

@unittest.skipUnless(importlib.util.find_spec('PIL'),'Install optional Pillow to test the CPU image runner')
class ImageRunnerTest(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.source=self.directory/'source.png'
        image=Image.new('RGB',(64,48));image.putdata([(80+x%70,100+y%60,120) for y in range(48) for x in range(64)])
        image.save(self.source);self.original=self.source.read_bytes()
    def tearDown(self):self.temp.cleanup()
    def test_basic_pipeline_outputs_version_and_preserves_source(self):
        from PIL import Image
        ai_runner.process({'source':str(self.source),'output':str(self.directory),'models':str(self.directory),'device':'cpu','operations':['basic'],'options':{}})
        output=json.loads((self.directory/'result.json').read_text())
        self.assertEqual(len(output),1);self.assertEqual(self.source.read_bytes(),self.original)
        with Image.open(output[0]['file']) as image:
            self.assertEqual(image.size,(64,48));self.assertEqual(image.mode,'RGB');self.assertIn('icc_profile',image.info)
        self.assertEqual(json.loads((self.directory/'progress.json').read_text())['progress'],100)
    def test_tiff_and_jpeg_export_preserve_dimensions_and_dpi(self):
        from PIL import Image
        for ext in ('tiff','jpg'):
            target=self.directory/('export.'+ext);ai_runner.save_rgb(ai_runner.read_rgb(self.source),target,600)
            with Image.open(target) as image:
                self.assertEqual(image.size,(64,48));self.assertEqual(image.mode,'RGB');self.assertAlmostEqual(image.info['dpi'][0],600,delta=1)
                self.assertIn('icc_profile',image.info)
    def test_real_isolated_cpu_process_reports_and_writes_result(self):
        import local_runtime,sys
        from unittest.mock import patch
        job={'id':'runner-test','source':str(self.source),'preset':'gentle','options':'{}'}
        stages=[]
        with patch('local_runtime.interpreter',return_value=sys.executable),patch('local_runtime.WORK',self.directory):
            results=local_runtime.run_job(job,'cpu',lambda value,stage:stages.append((value,stage)),lambda:False)
        self.assertEqual(len(results),1);self.assertTrue(Path(results[0]['file']).exists())
        self.assertEqual(self.source.read_bytes(),self.original)

    def test_large_result_is_bounded_without_changing_original(self):
        from PIL import Image
        Image.new('RGB',(6000,3000),(120,130,140)).save(self.source)
        original=self.source.read_bytes()
        ai_runner.process({'source':str(self.source),'output':str(self.directory),'models':str(self.directory),'device':'cpu','operations':['basic'],'options':{'denoise':False,'contrast':False,'sharpen':False}})
        output=json.loads((self.directory/'result.json').read_text())
        with Image.open(output[0]['file']) as image:self.assertLessEqual(image.width*image.height,16_000_000)
        self.assertEqual(self.source.read_bytes(),original)
if __name__=='__main__':unittest.main()
