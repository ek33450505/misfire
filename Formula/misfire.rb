class Misfire < Formula
  include Language::Python::Virtualenv

  desc "Trace-grounded CLAUDE.md adherence auditor: which prose rules your agents ignore"
  homepage "https://github.com/ek33450505/misfire"
  url "https://files.pythonhosted.org/packages/88/a8/a5d6cc4b5eb0e26ef28eb5b5d453d4fa9ba545dd8f0a7cd38d01f7daa047/misfire-0.1.0.tar.gz"
  sha256 "cfb2267466a2e56bf3aace00fc10ff9ce63df8f153da2ce3f838badbfe786b38"
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
