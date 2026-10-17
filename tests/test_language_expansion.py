"""Regression coverage for complete prepared packs and live command routing."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import string
import unittest
from unittest.mock import patch
from bot.languages import LANGUAGES, ALIASES, TEXT, HELP, COUNTRIES, translated, help_translation, bilingual, detect_language
import test_languages as fixtures

NEW = ('ja', 'pl', 'id', 'fr', 'de')

class PackTests(unittest.TestCase):
    def test_all_packs_cover_every_template_and_help_key(self):
        languages = list(LANGUAGES)[1:]
        values = dict(p='?', style='Variety', count='42', duration='1m 30s')
        formatter = string.Formatter()
        for catalog in (TEXT, HELP):
            for key, entries in catalog.items():
                self.assertEqual(len(entries), len(languages), key)
                expected = {name for _, name, _, _ in formatter.parse(entries[0]) if name}
                for language, entry in zip(languages, entries):
                    with self.subTest(language=language, key=key):
                        self.assertTrue(entry)
                        self.assertEqual({name for _, name, _, _ in formatter.parse(entry) if name}, expected)
                        rendered = entry.format(**values)
                        self.assertNotIn('{', rendered)
                        self.assertNotIn('\n', rendered)
                        self.assertLessEqual(len(rendered), 220)
                        fn = translated if catalog is TEXT else help_translation
                        self.assertEqual(fn(language, key, **values), rendered)

    def test_every_alias_maps_to_a_supported_language(self):
        for alias, code in ALIASES.items(): self.assertIn(code, LANGUAGES)
        for alias in ('日本語', 'polski', 'bahasa', 'français', 'deutsch'):
            self.assertIn(ALIASES[alias], NEW)

    def test_missing_help_or_templates_fall_back_for_new_languages(self):
        for code in NEW:
            self.assertEqual(help_translation(code, 'missing', p='.'), '')
            self.assertEqual(translated(code, 'missing'), '')
            self.assertEqual(bilingual('English', 'x'*400), 'English')

    def test_new_language_sentence_evidence(self):
        examples={'ja':'このマップはとても難しいです', 'pl':'nie rozumiem jak zmienić trudność',
                  'id':'saya tidak mengerti perintah ini', 'fr':'je ne comprends pas ces commandes',
                  'de':'ich verstehe diese Befehle nicht'}
        for code,text in examples.items():
            self.assertEqual(detect_language(text), code)
            self.assertIsNone(detect_language('.help '+text))
            self.assertIsNone(detect_language('https://example.com/'+text))

    def test_new_greetings_titles_and_conflicting_languages_are_not_offers(self):
        for text in ('こんにちは','ありがとう','cześć','terima kasih','bonjour','hallo',
                     'マップコマンド','je ne comprends pas ich verstehe nicht',
                     'saya tidak mengerti nie rozumiem komendy'):
            self.assertIsNone(detect_language(text), text)

    def test_new_country_hints_do_not_include_ambiguous_countries(self):
        for country,code in [('JP','ja'),('PL','pl'),('ID','id'),('FR','fr'),('DE','de')]:
            self.assertEqual(COUNTRIES[country],code)
        for country in ('US','CA','CH','BE','SG'): self.assertNotIn(country,COUNTRIES)

class ExpandedBotTests(unittest.TestCase):
    setUp = fixtures.LanguageBotTests.setUp
    send = fixtures.LanguageBotTests.send
    def test_time_alias_retains_all_personal_reply_languages(self):
        for i,code in enumerate(('zh','ko','es','ru',*NEW)):
            self.send(1,'.lang '+code,8000+i*10)
            self.assertEqual(self.send(1,'.t',8002+i*10),
                             self.send(1,'.time',8004+i*10))
            self.assertIn(' / ',self.send(1,'.t',8006+i*10))
    def test_new_codes_native_aliases_and_storage_survive_worker_replacement(self):
        for i,(code,alias) in enumerate(zip(NEW,('日本語','polski','bahasa','français','deutsch'))):
            reply=self.send(1,'.language '+alias,2000+i*10)
            self.assertIn(' / ',reply)
            self.assertEqual(self.bot._language_store.read(1)['language'],code)
            self.bot._init_player_languages()
            self.assertEqual(self.bot._language_store.read(1)['language'],code)
            self.assertIn(' / ',self.send(1,'.time',2002+i*10))
            self.assertEqual(self.send(2,'.time',2004+i*10),'No map running.')

    def test_new_codes_work_with_custom_prefix_and_reset(self):
        self.bot.prefix='?'
        for i,code in enumerate(NEW):
            self.assertIn(' / ',self.send(1,'?lang '+code,3000+i*10))
            self.assertIn('?lang en',self.send(1,'?lang',3002+i*10))
        self.assertIn('English-only',self.send(1,'?lang off',3100))
        self.assertEqual(self.send(1,'?time',3102),'No map running.')

    def test_current_language_usage_lists_every_code_and_is_translated(self):
        for i,code in enumerate(NEW):
            self.bot._language_store.choose(1,code)
            reply=self.send(1,'.lang',4000+i*10)
            self.assertIn('en|zh|ko|es|ru|ja|pl|id|fr|de',reply)
            self.assertIn(' / ',reply)
            self.assertLessEqual(len(reply),400)

    def test_focused_mania_help_and_acknowledgements_in_all_new_languages(self):
        self.bot.cfg.update(ruleset_id=3,mania_keys=4)
        for i,code in enumerate(NEW):
            self.bot._language_store.choose(1,code)
            for j,command in enumerate(('style','jack','ln','rice','noodle','map','start')):
                reply=self.send(1,'.help '+command,5000+i*50+j*2)
                self.assertIn(' / ',reply,(code,command))
                self.assertLessEqual(len(reply),400)
            self.bot._language_context=(1,['.variety'])
            english='Variety enabled—42 eligible maps. Future picks only.'
            result=self.bot._localize_command_reply(english)
            self.assertTrue(result.startswith(english+' / '))
            self.assertIn('42',result)
            self.bot._language_context=None

    def test_new_offers_require_two_sentences_and_do_not_choose_language(self):
        text='ich verstehe diese Befehle nicht'
        b=self.bot
        with patch('bot.bot.time.time',return_value=6000):
            b._observe_player_language(1,text,6000)
            self.assertFalse(b._maybe_language_offer())
        with patch('bot.bot.time.time',return_value=6010):
            b._observe_player_language(1,'kann ich bitte diese Schwierigkeit ändern',6010)
            self.assertTrue(b._maybe_language_offer())
        self.assertIn('.lang de',b.api.chat_send.call_args.args[1])
        self.assertIsNone(b._language_store.read(1)['language'])

    def test_new_country_offer_only_on_requested_help(self):
        for i,(country,code) in enumerate([('JP','ja'),('PL','pl'),('ID','id'),('FR','fr'),('DE','de')]):
            uid=20+i;b=self.bot;b.players[uid]={'name':'player','status':'idle','country_code':country}
            self.assertNotIn('Prefer',b._first_player_welcome(uid,'player'))
            reply=self.send(uid,'.help',7000+i*301)
            self.assertIn(' / ',reply)
            self.assertIn('.lang '+code,reply)
            self.assertIsNone(b._language_store.read(uid)['language'])

    def test_shared_messages_stay_english_for_each_new_language(self):
        for code in NEW:
            self.bot._language_store.choose(1,code)
            self.bot._language_context=(1,['.time'])
            self.assertEqual(self.bot._localize_command_reply('No map running.',automatic=True),'No map running.')
            self.assertEqual(self.bot._localize_command_reply('No map running.',lobby_only=True),'No map running.')
        self.bot._language_context=None

    def test_dt_acknowledgements_have_translation_without_changing_the_result(self):
        replies=[("dt", "dt enabled, buckle up gamers | Disable with .nm"),
                 ("dt", "DT 100% enabled; NoMod fallback | Mixed with .dt | Disable with .nm"),
                 ("nm", "DT disabled; NoMod picks only. A running DT map will finish."),
                 ("dt", "DT mixing is unavailable with this lobby's fixed mods.")]
        for code in list(LANGUAGES)[1:]:
            self.bot._language_store.choose(1,code)
            for cmd,english in replies:
                self.bot._language_context=(1,['.'+cmd])
                result=self.bot._localize_command_reply(english)
                self.assertTrue(result.startswith(english+' / '),(code,cmd))
                self.assertLessEqual(len(result),400)
        self.bot._language_context=None
