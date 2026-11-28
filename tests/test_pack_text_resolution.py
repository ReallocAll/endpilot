import json
import unittest

from endpilot import ResourcePackIndex, TextEvent, TextPacketType, TextTranslations


def text_object(node, *, kind=TextPacketType.TEXT_OBJECT_WHISPER, localize=False):
    return TextEvent(kind, json.dumps(node), localize)


class PackTextResolutionTests(unittest.TestCase):
    def setUp(self):
        self.index = ResourcePackIndex((), {
            "texts/en_US.lang": (
                "# fixture comment = ignored\r\n"
                "\r\n"
                "fixture.word=\u2603\r\n"
                "fixture.empty=\r\n"
                "fixture.blank=\t### deliberately empty\r\n"
                "fixture.space=  a=b #tag ##literal  \t### fixture comment\r\n"
                "fixture.percent=100% done\r\n"
                "fixture.seq=[%s] %s\r\n"
                "fixture.index=%%2 / %%1 / %%2\r\n"
                "fixture.ninth=%%9\r\n"
                "fixture.mixed=%s %%1\r\n"
                "fixture.zero=%%0\r\n"
                "fixture.tenth=%%10\r\n"
                "fixture.printf=%1$s\r\n"
            ).encode("utf-8"),
            "texts/fr_FR.lang": b"fixture.word=neige\n",
        })

    def resolve(self, node, *, locale="en_US"):
        return text_object(node).resolve_text(locale=locale, resource_pack_index=self.index)

    def test_literal_content_does_not_require_a_dictionary(self):
        cases = (
            (TextEvent(TextPacketType.CHAT, "  hello  ", False, author="Peer\u00a7r"), "<Peer>   hello  "),
            (TextEvent(TextPacketType.RAW, "  system\u00a7r ", False), "  system\u00a7r "),
            (TextEvent(TextPacketType.SYSTEM_MESSAGE, "", False), ""),
            (TextEvent(TextPacketType.TIP, "tip", False), "tip"),
            (text_object({"rawtext": [{"text": "  "}, {"rawtext": [{"text": "\u2603!"}]}]}), "  \u2603!"),
            (text_object({"rawtext": []}), ""),
        )
        for event, content in cases:
            with self.subTest(event=event):
                self.assertEqual(event.resolve_text(locale="en_US", resource_pack_index=None), content)

    def test_explicit_locale_and_lang_line_syntax(self):
        self.assertEqual(self.resolve({"translate": "fixture.word"}), "\u2603")
        self.assertEqual(self.resolve({"translate": "fixture.word"}, locale="fr_FR"), "neige")
        self.assertEqual(self.resolve({"translate": "fixture.space"}), "  a=b #tag ##literal  ")
        self.assertEqual(self.resolve({"translate": "fixture.empty"}), "")
        self.assertEqual(self.resolve({"translate": "fixture.blank"}), "")
        self.assertEqual(self.resolve({"translate": "fixture.percent"}), "100% done")
        self.assertIsNone(self.resolve({"translate": "fixture.space"}, locale="fr_FR"))
        for locale in ("en_GB", "en_us", "subdir/en_US"):
            with self.subTest(locale=locale):
                self.assertIsNone(self.resolve({"translate": "fixture.word"}, locale=locale))
        self.assertIsNone(text_object({"translate": "fixture.word"}).resolve_text(
            locale="en_US", resource_pack_index=None,
        ))

    def test_all_text_object_kinds_resolve_nested_rawtext_arguments(self):
        node = {"rawtext": [
            {"text": "start "},
            {"translate": "fixture.seq", "with": {"rawtext": [
                {"rawtext": [{"text": " "}, {"translate": "fixture.word"}]},
                {"translate": "fixture.index", "with": ["left", "right"]},
            ]}},
            {"text": " end"},
        ]}
        for kind in (TextPacketType.TEXT_OBJECT, TextPacketType.TEXT_OBJECT_WHISPER,
                     TextPacketType.TEXT_OBJECT_ANNOUNCEMENT):
            with self.subTest(kind=kind):
                event = text_object(node, kind=kind)
                self.assertEqual(event.resolve_text(locale="en_US", resource_pack_index=self.index),
                                 "start [ \u2603] right / left / right end")
                self.assertEqual(event.text_object, node)
        self.assertEqual(self.resolve({"translate": "fixture.seq", "with": ["%%1", "%s"]}), "[%%1] %s")
        self.assertEqual(self.resolve({"translate": "fixture.ninth", "with": [""] * 8 + ["last"]}), "last")

    def test_any_unresolved_node_or_argument_rejects_the_entire_event(self):
        for unresolved in (
            {"translate": "fixture.missing"},
            {"translate": "fixture.word", "text": "ambiguous"},
            {"translate": "fixture.word", "with": None},
            {"translate": "fixture.word", "with": ["extra"]},
            {"translate": "fixture.seq"},
            {"translate": "fixture.seq", "with": ["one"]},
            {"translate": "fixture.seq", "with": ["one", "two", "extra"]},
            {"translate": "fixture.seq", "with": ["one", 2]},
            {"translate": "fixture.index", "with": ["one"]},
            {"translate": "fixture.index", "with": {"rawtext": [
                {"text": "one"}, {"text": "two"}, {"translate": "fixture.missing"},
            ]}},
            {"translate": "fixture.mixed", "with": ["one"]},
            {"translate": "fixture.zero", "with": ["one"]},
            {"translate": "fixture.tenth", "with": ["one"] * 10},
            {"translate": "fixture.printf", "with": ["one"]},
            {"selector": "@s"},
            {"score": {"name": "player", "objective": "fixture"}},
        ):
            with self.subTest(unresolved=unresolved):
                self.assertIsNone(self.resolve({"rawtext": [{"text": "prefix"}, unresolved]}))

    def test_rejects_localized_packets_and_malformed_json(self):
        for event in (
            text_object({"translate": "fixture.word"}, localize=True),
            TextEvent(TextPacketType.CHAT, "literal", True, author="Peer"),
            TextEvent(TextPacketType.WHISPER, "literal", False, author="Peer"),
            TextEvent(TextPacketType.TEXT_OBJECT, '{"text":"one","text":"two"}', False),
            TextEvent(TextPacketType.TEXT_OBJECT,
                      '{"translate":"fixture.seq","with":{"rawtext":[{"text":"one","text":"two"}]}}',
                      False),
            TextEvent(TextPacketType.TEXT_OBJECT, '{"rawtext":[{"text":"one"}]', False),
        ):
            with self.subTest(event=event):
                self.assertIsNone(event.resolve_text(locale="en_US", resource_pack_index=self.index))

    def test_invalid_or_ambiguous_language_files_do_not_supply_translations(self):
        node = text_object({"translate": "fixture.word"})
        for lang in (
            b"fixture.word=valid\nfixture.other=\xff",
            b"\xef\xbb\xbffixture.word=valid",
            b"fixture.word=valid\nmissing delimiter",
            b"fixture.word=valid\nfixture.other = padded key",
            b"fixture.word=valid\nfixture.word=duplicate",
            b"fixture.word=valid\nfixture.word=valid",
        ):
            with self.subTest(lang=lang):
                index = ResourcePackIndex((), {"texts/en_US.lang": lang})
                self.assertIsNone(node.resolve_text(locale="en_US", resource_pack_index=index))

    def test_bounds_excessive_nesting_and_translation_growth(self):
        deep = {"text": "deep"}
        for _ in range(64):
            deep = {"rawtext": [deep]}
        self.assertIsNone(self.resolve(deep))

        repeat = {"text": "1234567890"}
        for _ in range(4):
            repeat = {"translate": "fixture.repeat", "with": {"rawtext": [repeat]}}
        index = ResourcePackIndex((), {"texts/en_US.lang": b"fixture.repeat=" + b"%%1" * 100})
        self.assertIsNone(text_object(repeat).resolve_text(locale="en_US", resource_pack_index=index))


class NativeTextTranslationsTests(unittest.TestCase):
    @staticmethod
    def native(key, *args, localize=True):
        return TextEvent(TextPacketType.TRANSLATE, key, localize, params=args)

    def test_formats_native_and_nested_rawtext_in_one_pass(self):
        translations = TextTranslations.from_lang(locale="en_US", vanilla_lang=(
            b"death.attack.lava=%1$s tried to swim in lava\n"
            b"multiplayer.player.left=%s left the game\n"
            b"record=%2$s / %1$s / %2$s\nraw=%%2 / %%1\n"
        ))
        death = self.native("death.attack.lava", "Pilot %2$s")
        self.assertEqual(translations.resolve(death), "Pilot %2$s tried to swim in lava")
        self.assertIsNone(death.resolve_text(locale="en_US", resource_pack_index=None))
        self.assertEqual((death.message, death.params), ("death.attack.lava", ("Pilot %2$s",)))
        self.assertEqual(translations.resolve(self.native("record", "one", "two")), "two / one / two")
        leave = text_object({"translate": "§e%multiplayer.player.left", "with": {"rawtext": [{"text": "Pilot"}]}})
        self.assertEqual(translations.resolve(leave), "§ePilot left the game")
        self.assertEqual(translations.resolve(self.native("§e%multiplayer.player.left", "Pilot")), "§ePilot left the game")
        self.assertEqual(translations.resolve(text_object({"translate": "raw", "with": ["one", "%s"]})), "%s / one")
        self.assertIsNone(translations.resolve(self.native("raw", "one", "two")))
        self.assertIsNone(translations.resolve(self.native("record", "one")))
        self.assertIsNone(translations.resolve(self.native("record", "one", "two", localize=False)))
        self.assertEqual(translations.resolve(TextEvent(TextPacketType.CHAT, " hi ", False, author="Peer§r")),
                         "<Peer>  hi ")

    def test_base_bom_and_unrelated_bad_entries_with_locale_overlay(self):
        base = (b"\xef\xbb\xbf\r\nkey=Base %s\r\nkept=100% done\r\nbad=\xff\n"
                b"dup=first\ndup=second\nmissing delimiter\nambiguous=valid\nambiguous=\xff\n")
        event = self.native("key", "Pilot")
        pack = ResourcePackIndex((), {"texts/fr_FR.lang": b"key=\n"})
        base_only = TextTranslations.from_lang(locale="en_US", vanilla_lang=base, resource_pack_index=pack)
        merged = TextTranslations.from_lang(locale="fr_FR", vanilla_lang=base, resource_pack_index=pack)
        self.assertEqual(base_only.resolve(event), "Base Pilot")
        self.assertEqual(merged.resolve(event), "")
        self.assertEqual(merged.resolve(self.native("kept")), "100% done")
        for key in ("bad", "dup", "ambiguous", "unlisted"):
            with self.subTest(key=key):
                self.assertIsNone(merged.resolve(self.native(key)))
        invalid = ResourcePackIndex((), {"texts/fr_FR.lang": b"key=Overlay %s\ndup=one\ndup=two"})
        unresolved = TextTranslations.from_lang(locale="fr_FR", vanilla_lang=base, resource_pack_index=invalid)
        self.assertIsNone(unresolved.resolve(event))
        self.assertIsNone(unresolved.resolve(self.native("kept")))
        self.assertEqual(unresolved.resolve(text_object({"text": "literal"})), "literal")
        self.assertEqual(merged.resolve(event), "")

    def test_official_language_layout_applies_to_base_and_overlay(self):
        base = b"\xef\xbb\xbf\r\nbase = kept\r\nbase.duplicate=one\r\nbase.duplicate =two\r\n"
        overlay = b"\xef\xbb\xbf\r\noverlay \t= padded value \t# comment\r\n"
        index = ResourcePackIndex((), {"texts/fr_FR.lang": overlay})
        translations = TextTranslations.from_lang(locale="fr_FR", vanilla_lang=base, resource_pack_index=index)
        self.assertEqual(translations.resolve(self.native("base")), " kept")
        self.assertIsNone(translations.resolve(self.native("base.duplicate")))
        event = text_object({"translate": "overlay"})
        self.assertEqual(translations.resolve(event), " padded value ")
        self.assertIsNone(event.resolve_text(locale="fr_FR", resource_pack_index=index))

        duplicate = ResourcePackIndex((), {"texts/fr_FR.lang": overlay + b"overlay=conflict\n"})
        translations = TextTranslations.from_lang(locale="fr_FR", vanilla_lang=base, resource_pack_index=duplicate)
        self.assertIsNone(translations.resolve(event))
        self.assertIsNone(translations.resolve(self.native("base")))

    def test_unresolved_template_or_rawtext_node_rejects_entire_message(self):
        translations = TextTranslations.from_lang(locale="en_US", vanilla_lang=(
            b"bad.printf=%2$d\nbad.mixed=%s %1$s\nbad.zero=%%0\nrequired=%2$s\n"
        ))
        for child in ({"translate": "bad.printf", "with": ["one", "two"]},
                      {"translate": "bad.mixed", "with": ["one"]},
                      {"translate": "bad.zero", "with": ["one"]},
                      {"translate": "required", "with": ["one"]},
                      {"translate": "missing"}, {"selector": "@s"}):
            with self.subTest(child=child):
                self.assertIsNone(translations.resolve(text_object({"rawtext": [{"text": "prefix"}, child]})))
        self.assertIsNone(translations.resolve(self.native("required", "one", "x" * (128 * 1024))))
