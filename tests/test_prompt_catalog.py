import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import unquote
import zipfile

from backend import materials, materials_api, prompt_catalog, storage
from tests import test_materials as fixture
PNG = fixture.PNG


class PromptCatalogTests(unittest.TestCase):
    setUp = fixture.MaterialsTests.setUp
    tearDown = fixture.MaterialsTests.tearDown

    def custom(self, kind="text", key="custom_" + "a" * 32):
        return {"id": key, "label": "Ответы на вопросы", "title": "Ответы на вопросы", "text": "Ответь на вопросы.", "type": kind, "enabled": True}

    def install(self, *items):
        state = prompt_catalog.get()
        return prompt_catalog.save(True, state["prompts"] + list(items), state["revision"])

    def test_defaults_are_unchanged_and_read_does_not_write_configuration(self):
        self.assertEqual([item["id"] for item in prompt_catalog.get()["prompts"]], ["summary", "minutes", "diagram"])
        self.assertFalse((self.root / "prompt-catalog.json").exists())

    def test_reorder_disable_delete_edit_survive_reload_and_keep_results(self):
        materials.save_text(self.project, "summary", "Существующий результат", 0)
        state = prompt_catalog.get()
        items = list(reversed(state["prompts"]))
        items[0]["enabled"] = False
        items[1]["text"] = "Новый текст промпта"
        result = prompt_catalog.save(False, items[:-1], 0)
        self.assertEqual(prompt_catalog.get(), result)
        self.assertEqual(materials.get(self.project)["summary"], "Существующий результат")
        self.assertEqual(result["prompts"][1]["text"], "Новый текст промпта")

    def test_conflicts_and_idempotent_retries(self):
        state = self.install(self.custom())
        self.assertEqual(prompt_catalog.save(True, state["prompts"], 0), state)
        with self.assertRaises(materials.Conflict):
            prompt_catalog.save(False, state["prompts"], 0)

    def test_types_cannot_change_even_after_deleting_button(self):
        state = self.install(self.custom())
        with self.assertRaises(ValueError):
            prompt_catalog.save(True, [self.custom("images")], state["revision"])
        removed = prompt_catalog.save(True, [], state["revision"])
        with self.assertRaises(ValueError):
            prompt_catalog.save(True, [self.custom("images")], removed["revision"])
        for item in prompt_catalog.defaults():
            item["type"] = "text" if item["type"] == "images" else "images"
            with self.assertRaises(ValueError):
                prompt_catalog.save(True, [item], removed["revision"])

    def test_custom_text_and_images_export_after_catalog_deletion(self):
        text = self.custom()
        image = self.custom("images", "custom_" + "b" * 32)
        self.install(text, image)
        state = materials.save_text(self.project, text["id"], "# Вопрос\n\nПолный ответ", 0)
        state = materials.add_image(self.project, "схема.png", PNG, state["revision"], image["id"])
        state = materials.add_image(self.project, "базовая.png", PNG, state["revision"])
        self.assertEqual(state["images"][0]["prompt_id"], image["id"])
        self.assertNotIn("prompt_id", state["images"][1])
        prompt_catalog.save(False, [], prompt_catalog.get()["revision"])
        self.assertIn("Полный ответ", materials.text_file(self.project, text["id"], "txt")[1].decode())
        files = materials.package_files(self.project, False, "txt", "none", "formatted", "off")
        self.assertEqual(len(files), 3)
        self.assertEqual(sum(content == PNG for _, content in files), 2)
        self.assertEqual(len({name for name, _ in files}), 3)
        materials.save_text(self.project, text["id"], "Обновлённый ответ", state["revision"])

    def test_custom_text_conflict_and_failed_export_preserve_manifest(self):
        item = self.custom()
        self.install(item)
        original = materials.save_text(self.project, item["id"], "Первый", 0)
        with self.assertRaises(materials.Conflict):
            materials.save_text(self.project, item["id"], "Старый", 0)
        with patch.object(materials, "_docx", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                materials.save_text(self.project, item["id"], "Не записан", 1)
        self.assertEqual(materials.get(self.project), original)

    def test_catalog_failure_keeps_previous_file(self):
        state = self.install(self.custom())
        with patch.object(prompt_catalog.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                prompt_catalog.save(False, state["prompts"], state["revision"])
        self.assertEqual(prompt_catalog.get(), state)

    def test_zip_uses_project_name_and_custom_prompt_api_uses_saved_text(self):
        item = self.custom()
        self.install(item)
        result = materials_api.prompt_input(self.project, item["id"], include_transcript=False)
        self.assertEqual(result["prompt"], item["text"])
        materials.save_text(self.project, item["id"], "Готово", 0)
        response = materials_api.package(self.project, "zip", include_transcript=False)
        self.assertIn("Тест материалов.zip", unquote(response.headers["content-disposition"]))
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(response.body)).namelist()), 1)

    def test_corrupt_catalog_and_invalid_ids_are_not_written(self):
        for key in ("../../x", "summary/evil", "custom_bad"):
            with self.assertRaises(ValueError):
                self.install(self.custom(key=key))
        path = self.root / "prompt-catalog.json"
        path.write_text('{"broken":true}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            prompt_catalog.get()
        self.assertEqual(path.read_text(), '{"broken":true}')
