class Misfire < Formula
  include Language::Python::Virtualenv

  desc "Trace-grounded CLAUDE.md adherence auditor: which prose rules your agents ignore"
  homepage "https://github.com/ek33450505/misfire"
  # TODO(release): fill url + sha256 from the PUBLISHED PyPI sdist after `uv publish`.
  #   url    — the sdist link from https://pypi.org/pypi/misfire/0.1.0/json
  #            (urls[] entry whose packagetype == "sdist", field "url")
  #   sha256 — that entry's digests.sha256, OR locally:
  #            shasum -a 256 dist/misfire-0.1.0.tar.gz   (valid only if the exact
  #            locally-built artifact is the one published)
  url "https://files.pythonhosted.org/packages/source/m/misfire/misfire-0.1.0.tar.gz"
  sha256 "REPLACE_WITH_PUBLISHED_SDIST_SHA256"
  license "Apache-2.0"

  depends_on "python@3.12"

  # Core is stdlib-only (zero runtime dependencies) — no resource stanzas needed.
  def install
    virtualenv_install_with_resources
  end

  test do
    # Version surface doubles as the install smoke test. (The proof/ fixtures are
    # intentionally excluded from the published artifact, so there is no `misfire proof`
    # to assert against — unlike looptrip.)
    assert_match "misfire #{version}", shell_output("#{bin}/misfire --version")
  end
end
