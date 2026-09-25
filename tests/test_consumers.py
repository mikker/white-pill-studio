import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adapters  # noqa: E402
import tokens_model  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "tokens.toml"
SCRIPT = ROOT / "script" / "design-system"
CLEAN_ENV = {key: value for key, value in os.environ.items()
             if key not in ("WHITE_PILL_DOTFILES", "WHITE_PILL_CONSUMERS")}


def parse(text, environ=None, base=None):
    return adapters.parse_consumers(tomllib.loads(text), environ or {}, base)


class ConsumerConfigTest(unittest.TestCase):
    def test_parses_names_roots_and_families(self):
        consumers = parse('''
[[consumer]]
name = "dotfiles"
root = "/srv/dotfiles"
adapters = ["omarchy-theme", "hunk"]

[[consumer]]
name = "laptop"
root = "relative/laptop"
adapters = ["hunk"]
''', base=pathlib.Path("/studio"))
        self.assertEqual([(c.name, str(c.root), c.families, c.origin) for c in consumers], [
            ("dotfiles", "/srv/dotfiles", ("omarchy-theme", "hunk"), "config"),
            ("laptop", "/studio/relative/laptop", ("hunk",), "config"),
        ])

    def test_expands_home_and_variables(self):
        consumers = parse('[[consumer]]\nname = "a"\nroot = "~/x"\n\n[[consumer]]\nname = "b"\nroot = "${BASE}/y/$SUB"\n',
                          {"BASE": "/base", "SUB": "z"})
        self.assertEqual(consumers[0].root, pathlib.Path("~/x").expanduser().resolve())
        self.assertEqual(consumers[1].root, pathlib.Path("/base/y/z"))

    def test_env_overrides_the_root(self):
        text = '[[consumer]]\nname = "dotfiles"\nroot = "/configured"\nenv = "WHITE_PILL_DOTFILES"\nadapters = ["hunk"]\n'
        self.assertEqual(parse(text)[0].root, pathlib.Path("/configured"))
        overridden = parse(text, {"WHITE_PILL_DOTFILES": "/elsewhere"})[0]
        self.assertEqual((overridden.root, overridden.origin), (pathlib.Path("/elsewhere"), "WHITE_PILL_DOTFILES"))
        self.assertEqual(parse(text, {"WHITE_PILL_DOTFILES": ""})[0].root, pathlib.Path("/configured"))

    def test_rejects_bad_configuration(self):
        cases = (
            ('[[consumer]]\nname = "x"\nroot = "/r"\nadapters = ["pi"]\n', "unknown adapter family pi"),
            ('[[consumer]]\nname = "x"\nroot = "/r"\n\n[[consumer]]\nname = "x"\nroot = "/s"\n', "duplicate"),
            ('[[consumer]]\nname = "studio"\nroot = "/r"\n', "reserved"),
            ('[[consumer]]\nname = "x"\n', "root must be a path"),
            ('[[consumer]]\nname = "x"\nroot = "/r"\nextra = 1\n', "unknown key"),
            ('[[consumer]]\nroot = "/r"\n', "name must be"),
            ('[consumer]\nname = "x"\n', r"\[\[consumer\]\]"),
        )
        for text, message in cases:
            with self.assertRaisesRegex(adapters.ConsumerError, message, msg=text):
                parse(text)

    def test_load_defaults_without_a_file_and_keeps_the_dotfiles_override(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = adapters.load_consumers(pathlib.Path(directory), {"WHITE_PILL_DOTFILES": "/legacy"})
            self.assertEqual([c.name for c in loaded], ["studio", "dotfiles"])
            self.assertEqual(loaded[0].root, pathlib.Path(directory).resolve())
            self.assertEqual((loaded[1].root, loaded[1].families), (pathlib.Path("/legacy"), adapters.FAMILIES))
            default = adapters.load_consumers(pathlib.Path(directory), {})[1]
            self.assertEqual((default.root, default.origin), (pathlib.Path("~/.dotfiles").expanduser().resolve(), "default"))

    def test_repository_configuration_matches_the_default(self):
        loaded = adapters.load_consumers(ROOT, {})
        self.assertEqual([c.name for c in loaded], ["studio", "dotfiles"])
        self.assertEqual(set(loaded[1].families), set(adapters.FAMILIES))
        self.assertEqual(adapters.load_consumers(ROOT, {"WHITE_PILL_DOTFILES": "/x"})[1].root, pathlib.Path("/x"))

    def test_consumers_file_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "elsewhere.toml"
            config.write_text('[[consumer]]\nname = "other"\nroot = "/o"\nadapters = ["hunk"]\n')
            loaded = adapters.load_consumers(ROOT, {"WHITE_PILL_CONSUMERS": str(config)})
            self.assertEqual([c.name for c in loaded], ["studio", "other"])


class ConsumerTargetsTest(unittest.TestCase):
    def test_targets_follow_each_consumers_families(self):
        raw = tokens_model.load(FIXTURE)
        consumers = [
            adapters.Consumer("studio", pathlib.Path("/s"), ("studio-css",)),
            adapters.Consumer("dotfiles", pathlib.Path("/d"), adapters.FAMILIES),
            adapters.Consumer("review", pathlib.Path("/r"), ("hunk",)),
        ]
        outputs = adapters.outputs(raw, consumers)
        review = [str(path) for path in outputs if str(path).startswith("/r/")]
        self.assertEqual(review, ["/r/hunk/config.dark.toml", "/r/hunk/config.light.toml"])
        self.assertEqual(outputs[pathlib.Path("/r/hunk/config.dark.toml")], outputs[pathlib.Path("/d/hunk/config.dark.toml")])
        manifest = adapters.manifest(raw, pathlib.Path("/s"), consumers)
        self.assertEqual([c["name"] for c in manifest["consumers"]], ["studio", "dotfiles", "review"])
        hunk = next(t for t in manifest["targets"] if t["path"] == "/r/hunk/config.light.toml")
        self.assertEqual((hunk["consumer"], hunk["root"], hunk["display"], hunk["family"]),
                         ("review", "/r", "review:hunk/config.light.toml", "hunk"))


class Workspace:
    """A temporary studio root with the fixture tokens and two consumers."""

    def __init__(self, consumers_toml):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        self.source = self.root / "design-system" / "tokens.toml"
        self.source.parent.mkdir()
        self.source.write_text(FIXTURE.read_text())
        (self.root / "design-system" / "consumers.toml").write_text(consumers_toml.format(root=self.root))

    def run(self, *args, env=None):
        # The fontconfig family check depends on the machine's fonts; these
        # tests exercise the CLI, not the font set of whoever runs them.
        run_env = {**(env or CLEAN_ENV), "WHITE_PILL_FONT_CHECK": "0"}
        return subprocess.run([sys.executable, str(SCRIPT), "--source", str(self.source), *args],
                              capture_output=True, text=True, env=run_env, cwd=self.root)

    def close(self):
        self.directory.cleanup()


TWO_CONSUMERS = '''
[[consumer]]
name = "dotfiles"
root = "{root}/dotfiles"
env = "TEST_DOTFILES"
adapters = ["omarchy-plugin", "omarchy-theme", "hunk"]

[[consumer]]
name = "review"
root = "{root}/review"
adapters = ["hunk"]
'''


class MultiConsumerCommandTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace(TWO_CONSUMERS)
        (self.workspace.root / "dotfiles").mkdir()
        (self.workspace.root / "review").mkdir()

    def tearDown(self):
        self.workspace.close()

    def test_generate_then_check_every_consumer(self):
        generated = self.workspace.run("generate")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertIn("review:hunk/config.dark.toml", generated.stdout.splitlines())
        self.assertIn("studio:design-system/tokens.css", generated.stdout.splitlines())
        checked = self.workspace.run("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        lines = checked.stdout.splitlines()
        self.assertRegex(lines[0], r"^studio +\S+ +current \(1 file\)$")
        self.assertRegex(lines[1], r"^dotfiles +\S+/dotfiles +current \(29 files\)$")
        self.assertRegex(lines[2], r"^review +\S+/review +current \(2 files\)$")
        self.assertEqual(lines[-1], "Design-system outputs are current")

    def test_stale_files_are_listed_by_consumer(self):
        self.workspace.run("generate")
        (self.workspace.root / "review" / "hunk" / "config.light.toml").write_text("edited by hand\n")
        (self.workspace.root / "dotfiles" / "hunk" / "config.dark.toml").unlink()
        checked = self.workspace.run("check")
        self.assertEqual(checked.returncode, 1)
        self.assertRegex(checked.stdout, r"studio .* current")
        self.assertRegex(checked.stderr, r"dotfiles +\S+ +1 of 29 file\(s\) stale")
        self.assertRegex(checked.stderr, r"review +\S+ +1 of 2 file\(s\) stale")
        self.assertIn("  dotfiles:hunk/config.dark.toml", checked.stderr)
        self.assertIn("  review:hunk/config.light.toml", checked.stderr)

    def test_missing_root_names_the_consumer(self):
        (self.workspace.root / "review").rmdir()
        checked = self.workspace.run("check")
        self.assertEqual(checked.returncode, 1)
        self.assertIn("consumer 'review': root", checked.stderr)
        self.assertIn("does not exist", checked.stderr)
        generated = self.workspace.run("generate")
        self.assertEqual(generated.returncode, 1)
        self.assertIn("consumer 'review'", generated.stderr)
        self.assertFalse((self.workspace.root / "dotfiles" / "hunk").exists(), "generate must not write partially")

    def test_env_override_redirects_one_consumer(self):
        elsewhere = self.workspace.root / "elsewhere"
        elsewhere.mkdir()
        env = {**CLEAN_ENV, "TEST_DOTFILES": str(elsewhere)}
        self.assertEqual(self.workspace.run("generate", env=env).returncode, 0)
        self.assertTrue((elsewhere / "hunk" / "config.dark.toml").exists())
        self.assertFalse((self.workspace.root / "dotfiles" / "hunk").exists())

    def test_manifest_and_consumers_commands(self):
        manifest = json.loads(self.workspace.run("manifest").stdout)
        self.assertEqual([c["name"] for c in manifest["consumers"]], ["studio", "dotfiles", "review"])
        review = [t for t in manifest["targets"] if t["consumer"] == "review"]
        self.assertEqual([t["display"] for t in review], ["review:hunk/config.dark.toml", "review:hunk/config.light.toml"])
        self.assertEqual(review[0]["root"], str((self.workspace.root / "review").resolve()))

        listed = self.workspace.run("consumers", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = json.loads(listed.stdout)["consumers"]
        self.assertEqual([(row["name"], row["exists"]) for row in rows], [("studio", True), ("dotfiles", True), ("review", True)])
        self.assertIsNone(rows[2]["git"])
        subprocess.run(["git", "init", "-q", str(self.workspace.root / "review")], check=True)
        rows = json.loads(self.workspace.run("consumers", "--json").stdout)["consumers"]
        self.assertEqual(rows[2]["git"], {"head": None, "dirty": False})
        text = self.workspace.run("consumers").stdout
        self.assertRegex(text, r"review +\S+ +git \(no commits\) clean +← +hunk")


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace('[[consumer]]\nname = "dotfiles"\nroot = "{root}/dotfiles"\nadapters = ["hunk"]\n')
        (self.workspace.root / "dotfiles").mkdir()
        self.assertEqual(self.workspace.run("generate").returncode, 0)

    def tearDown(self):
        self.workspace.close()

    def test_bumps_the_version_line_and_prints_the_tag(self):
        before = self.workspace.source.read_text()
        result = self.workspace.run("release", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.workspace.source.read_text()
        self.assertEqual(after, before.replace("\nversion = 1\n", "\nversion = 2\n", 1))
        self.assertEqual(tokens_model.load(self.workspace.source)["meta"]["version"], 2)
        self.assertIn("Bumped [meta] version 1 → 2", result.stdout)
        self.assertIn(f"git -C {self.workspace.root} tag design-system/v2", result.stdout)
        self.assertEqual(self.workspace.run("check").returncode, 0)

    def test_refuses_older_versions_wrong_types_and_drift(self):
        for version, message in (("0", "older"), ("1.5", "integer"), ("v-x", "same type")):
            result = self.workspace.run("release", version)
            self.assertEqual(result.returncode, 1, version)
            self.assertIn(message, result.stderr)
        (self.workspace.root / "dotfiles" / "hunk" / "config.dark.toml").write_text("drift\n")
        result = self.workspace.run("release", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale", result.stderr)
        self.assertEqual(tokens_model.load(self.workspace.source)["meta"]["version"], 1)

    def test_tag_requires_commits_a_clean_tree_and_a_committed_bump(self):
        result = self.workspace.run("release", "1", "--tag")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no commits", result.stderr)

        root = str(self.workspace.root)
        git_env = {**CLEAN_ENV, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}
        (self.workspace.root / ".gitignore").write_text("dotfiles/\n")
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", "-C", root, *command], check=True, env=git_env, capture_output=True)

        result = self.workspace.run("release", "2", "--tag", env=git_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("without --tag", result.stderr)
        self.assertEqual(self.workspace.run("release", "2", env=git_env).returncode, 0)
        result = self.workspace.run("release", "2", "--tag", env=git_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted", result.stderr)
        subprocess.run(["git", "-C", root, "commit", "-qam", "v2"], check=True, env=git_env)
        result = self.workspace.run("release", "2", "--tag", env=git_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        tags = subprocess.run(["git", "-C", root, "tag"], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(tags, ["design-system/v2"])
        result = self.workspace.run("release", "2", "--tag", env=git_env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)


if __name__ == "__main__":
    unittest.main()
