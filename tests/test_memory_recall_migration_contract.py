import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260817020000_memory_recall_metadata_and_keywords.sql"

METADATA_FIELDS = (
    "id",
    "content",
    "title",
    "tags",
    "heat",
    "importance",
    "layer",
    "created_at",
    "last_recalled_at",
    "memory_type",
    "continuity_type",
    "subject",
    "source_type",
    "thread_state",
    "continuity_value",
    "retention_class",
    "participants",
    "memory_time",
    "evidence_start_time",
    "evidence_end_time",
)


class MemoryRecallMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.vector_section, cls.keyword_section = cls.executable.split(
            "create or replace function public.search_memories_by_keywords",
            1,
        )

    def test_vector_function_is_dropped_by_exact_signature_without_cascade(self):
        self.assertRegex(
            self.vector_section,
            r"drop\s+function\s+if\s+exists\s+public\.match_memories\s*\(\s*extensions\.vector\s*,\s*double precision\s*,\s*integer\s*\)",
        )
        self.assertNotRegex(self.executable, r"\bcascade\b")
        self.assertRegex(self.executable, r"\bbegin\s*;")
        self.assertRegex(self.executable, r"\bcommit\s*;")

    def test_vector_channel_returns_layer_similarity_and_continuity_metadata(self):
        for field in (*METADATA_FIELDS, "similarity"):
            self.assertRegex(self.vector_section, rf"\b{field}\b")
        self.assertIn("memory.embedding is not null", self.vector_section)
        self.assertIn("memory.is_active = true", self.vector_section)
        self.assertIn("memory.verified = 'verified'", self.vector_section)
        self.assertRegex(self.vector_section, r"coalesce\(match_threshold,\s*0\.5\)")
        self.assertRegex(self.vector_section, r"coalesce\(match_count,\s*20\)")
        self.assertRegex(self.vector_section, r"limit\s+least\(.+50\)")

    def test_keyword_channel_matches_content_title_and_unnested_tags(self):
        self.assertRegex(self.keyword_section, r"memory\.content")
        self.assertRegex(self.keyword_section, r"memory\.title")
        self.assertRegex(
            self.keyword_section,
            r"unnest\(coalesce\(memory\.tags,\s*'\{\}'::text\[\]\)\)",
        )
        self.assertNotIn("tags.ilike", self.keyword_section)
        for field in METADATA_FIELDS:
            self.assertRegex(self.keyword_section, rf"\b{field}\b")

    def test_keyword_inputs_and_results_are_bounded(self):
        self.assertRegex(self.keyword_section, r"input\.position\s*<=\s*5")
        self.assertRegex(self.keyword_section, r"left\(btrim\(input\.keyword\),\s*64\)")
        self.assertRegex(self.keyword_section, r"between\s+1\s+and\s+64")
        self.assertRegex(self.keyword_section, r"coalesce\(result_limit,\s*20\)")
        self.assertRegex(self.keyword_section, r"limit\s+least\(.+50\)")

    def test_both_channels_are_read_only_verified_active_and_server_only(self):
        for section in (self.vector_section, self.keyword_section):
            self.assertIn("memory.is_active = true", section)
            self.assertIn("memory.verified = 'verified'", section)
            self.assertRegex(section, r"\bstable\b")
            self.assertRegex(section, r"set\s+search_path")
        for function_name in ("match_memories", "search_memories_by_keywords"):
            self.assertRegex(
                self.executable,
                rf"revoke\s+all\s+on\s+function\s+public\.{function_name}[\s\S]+?from\s+public,\s*anon,\s*authenticated",
            )
            self.assertRegex(
                self.executable,
                rf"grant\s+execute\s+on\s+function\s+public\.{function_name}[\s\S]+?to\s+service_role",
            )

    def test_migration_does_not_modify_tables_or_chat_messages(self):
        self.assertNotRegex(self.executable, r"alter\s+table")
        self.assertNotRegex(self.executable, r"(?:insert\s+into|update|delete\s+from|truncate)\s+public\.")
        self.assertNotRegex(
            self.executable,
            r"(?:insert\s+into|update|delete\s+from|alter\s+table|drop\s+table|truncate)[\s\S]{0,80}chat_messages",
        )


if __name__ == "__main__":
    unittest.main()
