import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'core'))

import setup


class SetupVideoMappingTests(unittest.TestCase):
    def test_adult_mapping_is_removed_on_toggle_and_manual_destination_survives(self):
        policy = {'mapping': {}, '_adultSeparated': True,
                  '_setupGeneratedMapping': {'adult_audio': '成人内容', 'adult_game': '成人内容', 'adult_image': '成人内容'}}
        setup.update_setup_mapping(policy, 'adult_separation', '分开，成人内容独立成区', '混在一起')
        self.assertEqual(policy['mapping'], {})
        policy = {'mapping': {'adult_audio': '我的私密区'},
                  '_setupGeneratedMapping': {'adult_game': '成人内容'}}
        setup.update_setup_mapping(policy, 'adult_separation', '分开', '混在一起')
        self.assertEqual(policy['mapping'], {'adult_audio': '我的私密区'})

    def test_tool_product_mapping_is_removed_when_toggled_back(self):
        policy = {'mapping': {'voice_synth': '歌声合成', 'music': '自定义音乐', 'system_image': '系统镜像'},
                  '_setupGeneratedMapping': {'voice_synth': '歌声合成'}}
        setup.update_setup_mapping(policy, 'tool_vs_product', '分开', '不分开')
        self.assertEqual(policy['mapping'], {'music': '自定义音乐', 'system_image': '系统镜像'})
        policy['mapping']['voice_synth'] = '歌声合成'
        setup.update_setup_mapping(policy, 'tool_vs_product', '不分开', '分开')
        setup.update_setup_mapping(policy, 'tool_vs_product', '分开', '不分开')
        self.assertEqual(policy['mapping']['voice_synth'], '歌声合成')

    def test_null_adult_answer_does_not_raise(self):
        with tempfile.TemporaryDirectory(prefix='organizer-test-setup-') as temp:
            root = Path(temp) / 'disk'
            root.mkdir()
            answers = Path(temp) / 'answers.json'
            answers.write_text(json.dumps({'setup': {'adult_separation': None}}), encoding='utf-8')
            self.assertEqual(setup.main(['apply', str(root), str(answers)]), 1)
            self.assertFalse((root / '_organize' / 'policy.json').exists())

    def test_changing_video_split_removes_generated_but_keeps_custom_mapping(self):
        with tempfile.TemporaryDirectory(prefix='organizer-test-setup-') as temp:
            root = Path(temp) / 'disk'
            root.mkdir()
            org = root / '_organize'
            org.mkdir()
            policy_path = org / 'policy.json'
            policy_path.write_text(json.dumps({
                'setup': {'video_split': '分开：影视 / 学习资料 / 娱乐视频'},
                'mapping': {
                    'video_movie': '影视', 'video_series': '自定义动画',
                    'video_course': '学习资料', 'video_clip': '娱乐视频',
                    'video_custom': '收藏',
                },
            }), encoding='utf-8')
            answers = Path(temp) / 'answers.json'
            answers.write_text(json.dumps({'setup': {'video_split': '全放一起'}}), encoding='utf-8')
            self.assertEqual(setup.main(['apply', str(root), str(answers)]), 0)
            saved = json.loads(policy_path.read_text(encoding='utf-8'))
            mapping = saved['mapping']
            self.assertEqual({mapping[key] for key in ('video_movie', 'video_course', 'video_clip')}, {'视频'})
            self.assertEqual(mapping['video_series'], '自定义动画')
            self.assertEqual(mapping['video_custom'], '收藏')

    def test_generated_video_mapping_yields_to_manual_edits_on_next_change(self):
        policy = {'mapping': {'video_movie': '影视'}}
        setup.update_video_mapping(policy, None, '分开：影视 / 学习资料 / 娱乐视频')
        policy['mapping']['video_series'] = '我的动画'
        setup.update_video_mapping(policy, '分开：影视 / 学习资料 / 娱乐视频', '全放一起')
        self.assertEqual(policy['mapping'], {
            'video_movie': '影视', 'video_series': '我的动画', 'video_course': '视频', 'video_clip': '视频',
        })
        setup.update_video_mapping(policy, '全放一起', '只分影视和其他')
        self.assertEqual(policy['mapping']['video_course'], '其他视频')
        self.assertEqual(policy['mapping']['video_clip'], '其他视频')


if __name__ == '__main__':
    unittest.main(verbosity=2)
