""".NET support: C# snippet shapes, the wrapper, the HEAD surface, NuGet
detection and versions, and the compiler-error judgement.

The judgement tests each encode a false positive measured on the Duende
corpus (1,295 C# snippets) before the rule that stops it existed.
"""
from __future__ import annotations

import json
import textwrap

from docrot.acquire import registry
from docrot.acquire.repo import dotnet_surface, find_package, nuget_packages
from docrot.extract import csharp, pipeline, symbols, tiers
from docrot.models import PackageRef, Release, Snippet
from docrot.verify import dotnet
from docrot.verify.dotnet import Build, Compiled, Diagnostic, Surface


def _snip(code: str, sid: str = "s1") -> Snippet:
    s = Snippet(id=sid, page="p", lang="csharp", code=textwrap.dedent(code).strip())
    s.kind, s.declares = csharp.classify(s.code)
    return s


# ------------------------------------------------------------------ lexing
def test_mask_blanks_comments_and_strings_but_keeps_offsets():
    code = 'var s = "a;b}"; // c;}\n/* x{ */ var t = @"q""{";\nvar c = \'}\';'
    m = csharp.mask(code)
    assert len(m) == len(code) and m.count("\n") == code.count("\n")
    assert "}" not in m and "{" not in m
    assert m.count(";") == 3


def test_items_split_at_depth_zero_and_keep_constructs_whole():
    its = csharp.items(textwrap.dedent("""
        using Duende.IdentityServer;
        var options = new Options { A = 1, B = 2 };
        if (x) { Go(); } else { Stop(); }
        public class Foo { public int X { get; set; } }
        public string Name { get; set; }
        app.MapGet("/", () => { return 1; });
    """).strip())
    assert [i.kind for i in its] == ["using", "statement", "statement", "type",
                                     "member", "statement"]
    assert its[2].text.endswith("Stop(); }")


def test_classify_declarations_and_programs():
    assert csharp.classify("public class A { }\npublic interface IB { }")[0] == "declaration"
    kind, decls = csharp.classify("builder.Services.AddIdentityServer();")
    assert kind == "program" and decls == []
    assert csharp.classify("using X;")[0] == "fragment"
    assert [d["name"] for d in csharp.classify("public record R(int A);")[1]] == ["R"]


# ------------------------------------------------------------------ wrapping
def test_wrap_lifts_usings_and_maps_lines_back_to_the_docs():
    w = csharp.wrap(textwrap.dedent("""
        using Duende.IdentityServer;

        var builder = WebApplication.CreateBuilder(args);
        builder.Services.AddIdentityServer();
    """).strip(), "DocRot.S1")
    lines = w.source.split("\n")
    assert lines[0] == "using Duende.IdentityServer;"
    # the snippet declares `builder` itself, so no ambient parameter shadows it
    assert "WebApplicationBuilder builder" not in w.source
    assert "string[] args" in w.source
    call = next(i for i, ln in enumerate(lines, 1) if "AddIdentityServer" in ln)
    assert w.lines[call] == 4


def test_wrap_separates_types_members_and_statements():
    w = csharp.wrap(textwrap.dedent("""
        namespace Mine;
        public class Profile : IProfileService { }
        private readonly IUserStore _users;
        services.AddTransient<IProfileService, Profile>();
    """).strip(), "DocRot.S2")
    assert "namespace Mine;" not in w.source          # cannot nest in ours
    assert "public partial class __Members" in w.source
    assert w.source.index("class Profile") < w.source.index("__Members") < w.source.index("__Run")
    assert "IServiceCollection services" in w.source


def test_elision_is_detected_outside_strings_only():
    assert csharp.has_elision("services.AddX(...);")
    assert csharp.has_elision("options => { … }")
    assert not csharp.has_elision('Log("loading...");')
    assert not csharp.has_elision("var r = x?.y;")


# ------------------------------------------------------------ HEAD surface
def test_surface_credits_members_to_the_enclosing_type():
    api = csharp.surface([textwrap.dedent("""
        namespace A.B
        {
            public class Foo
            {
                public int Bar { get; set; }
                public class Inner
                {
                    public int X;
                }
                public void Baz(int x) {}
            }
        }
    """)])
    assert api["types"] == ["Foo"]                      # nested types are not top-level
    assert api["members"]["Foo"] == ["Bar", "Baz"]      # Baz is Foo's, after Inner closes


def test_surface_keys_extensions_by_receiver_including_csharp14_blocks():
    api = csharp.surface([textwrap.dedent("""
        namespace X;
        public static class E
        {
            extension(SchemaId)
            {
                public static SchemaId Client => null;
            }
            extension(IIdentityServerBuilder builder)
            {
                public IIdentityServerBuilder AddStorage() => builder;
            }
            public static IApplicationBuilder UseIdentityServer(this IApplicationBuilder app) => app;
        }
    """)])
    assert api["extensions"] == {"SchemaId": ["Client"],
                                 "IIdentityServerBuilder": ["AddStorage"],
                                 "IApplicationBuilder": ["UseIdentityServer"]}
    s = Surface.of(api)
    assert s.extends("WebApplication", "UseIdentityServer")    # via IApplicationBuilder
    assert not s.extends("HttpRequest", "Client")              # a SchemaId extension


# ------------------------------------------------------------- NuGet repos
def _csproj(path, body: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>{body}'
                    "</PropertyGroup></Project>")


def test_nuget_packages_skip_tests_samples_and_apps(tmp_path):
    _csproj(tmp_path / "src/Core/Core.csproj", "<PackageId>Acme.Core</PackageId>")
    _csproj(tmp_path / "src/Web/Web.csproj", "<PackageId>Acme.Web</PackageId>"
            "<RootNamespace>Acme.AspNetCore</RootNamespace>")
    _csproj(tmp_path / "test/Core.Tests/Core.Tests.csproj")
    _csproj(tmp_path / "samples/Demo/Demo.csproj")
    _csproj(tmp_path / "src/Tool/Tool.csproj", "<OutputType>Exe</OutputType>")
    _csproj(tmp_path / "src/Internal/Internal.csproj", "<IsPackable>false</IsPackable>")
    found = {p.id: p.namespace for p in nuget_packages(tmp_path)}
    assert found == {"Acme.Core": "Acme.Core", "Acme.Web": "Acme.AspNetCore"}


def test_a_clients_folder_disqualifies_only_projects_without_a_package_id(tmp_path):
    _csproj(tmp_path / "clients/src/ConsoleCode/ConsoleCode.csproj")          # Duende's sample client
    _csproj(tmp_path / "clients/dotnet/src/Relate.Net/Relate.Net.csproj", "<PackageId>Relate.Net</PackageId>")
    _csproj(tmp_path / "samples/Demo/Demo.csproj", "<PackageId>Acme.Demo</PackageId>")   # samples never
    assert [p.id for p in nuget_packages(tmp_path)] == ["Relate.Net"]


def test_root_namespace_is_spelled_as_the_code_declares_it(tmp_path):
    _csproj(tmp_path / "src/Bff/Bff.csproj", "<AssemblyName>Acme.BFF</AssemblyName>")
    (tmp_path / "src/Bff/Endpoints.cs").write_text("namespace Acme.Bff.Endpoints;\n")
    assert [(p.id, p.namespace) for p in nuget_packages(tmp_path)] == [("Acme.BFF", "Acme.Bff")]


def test_find_package_picks_the_named_or_most_mentioned_nuget_package(tmp_path):
    _csproj(tmp_path / "src/Core/Core.csproj", "<PackageId>Acme.Core</PackageId>")
    _csproj(tmp_path / "src/Web/Web.csproj", "<PackageId>Acme.Web</PackageId>")
    docs = "Acme.Web Acme.Web Acme.Core"
    assert find_package(tmp_path, want="Acme.Core")[:3] == ("Acme.Core", "Acme.Core", "nuget")
    assert find_package(tmp_path, rank=docs.count)[0] == "Acme.Web"
    assert find_package(tmp_path, rank=docs.count)[3] == tmp_path / "src/Web"


def test_head_surface_follows_project_references(tmp_path):
    _csproj(tmp_path / "src/Storage/Storage.csproj", "<PackageId>Acme.Storage</PackageId>")
    (tmp_path / "src/Storage/Client.cs").write_text(
        "namespace Acme.Models;\npublic class Client\n{\n    public string ClientId { get; set; }\n}\n")
    main = tmp_path / "src/Core/Core.csproj"
    main.parent.mkdir(parents=True)
    main.write_text('<Project><ItemGroup><ProjectReference Include="..\\Storage\\Storage.csproj" />'
                    "</ItemGroup></Project>")
    (tmp_path / "src/Core/Server.cs").write_text("namespace Acme;\npublic class Server { }\n")
    api = dotnet_surface(main.parent)
    assert set(api["types"]) == {"Client", "Server"}
    assert api["members"]["Client"] == ["ClientId"]


def test_nuget_versions_spread_across_major_lines():
    rels = [Release(label=v, released_at=d) for v, d in [
        ("7.3.4", "2025-12-29"), ("7.4.11", "2026-08-01"), ("7.4.12", "2026-09-15"),
        ("8.0.6", "2026-08-13"), ("8.0.7", "2026-09-03"), ("8.0.8", "2026-09-15")]]
    # by default: three patches of one minor, which agree on almost everything
    assert [r.label for r in registry.pick_versions(rels, 3)] == ["8.0.6", "8.0.7", "8.0.8"]
    # one per major line, newest last - 7.4.12 shipped the same day as 8.0.8
    picked = registry.pick_versions(rels, 3, spread=True)
    assert [r.label for r in picked] == ["7.3.4", "7.4.12", "8.0.8"]


# -------------------------------------------------------------- extraction
def test_csharp_snippets_are_compiled_and_attributed_by_namespace():
    s = Snippet(id="a", page="p", lang="csharp",
                code="using Acme.Web.Auth;\nusing Other.Thing;\nservices.AddAcme();")
    symbols.annotate([s], ["Acme.Core", "Acme.Web"])
    assert s.symbols == ["Acme.Web.Auth"]
    tiers.assign([s], set(), ["Acme.Core", "Acme.Web"])
    assert s.tier == "structural" and s.kind == "program"
    pkgs = [PackageRef(package="Acme.Core", import_name="Acme.Core", ecosystem="nuget"),
            PackageRef(package="Acme.Web", import_name="Acme.Web", ecosystem="nuget")]
    pipeline.attribute([s], pkgs)
    assert s.package == "Acme.Web"


def test_snippets_only_verify_against_their_own_language():
    cs = Snippet(id="c", page="p", lang="csharp", code="Go();", tier="structural", package="py")
    py = Snippet(id="y", page="p", lang="python", code="go()", tier="structural", package="net")
    pipeline.match_languages([cs, py], [PackageRef(package="py", import_name="py"),
                                        PackageRef(package="net", import_name="Net",
                                                   ecosystem="nuget")])
    assert cs.tier == py.tier == "unverifiable"


# ---------------------------------------------------------------- judging
def _surface(types=(), members=None, ns=(), ext=None) -> Surface:
    return Surface(set(ns), set(types), {k: set(v) for k, v in (members or {}).items()},
                   {k: set(v) for k, v in (ext or {}).items()})


def _err(code: str, msg: str, line: int = 1) -> Diagnostic:
    return Diagnostic(code=code, severity="error", message=msg, line=line)


def _compiled(surface: Surface, diags: list[Diagnostic] | None = None, sid="s1",
              syntax: list[Diagnostic] | None = None) -> Compiled:
    return Compiled(surface, {sid: diags} if diags else {}, {sid: syntax} if syntax else {})


RENAMED = "The type or namespace name 'DPoPProofValidatonContext' could not be found"


def test_an_error_only_the_newer_release_raises_is_drift():
    s = _snip("Task<bool> Validate(DPoPProofValidatonContext context);")
    old = _surface(types={"DPoPProofValidatonContext"})
    new = _surface(types={"DPoPProofValidationContext"})
    builds = {"7.4.12": _compiled(old), "8.0.8": _compiled(new, [_err("CS0246", RENAMED)])}
    status, record = dotnet.judge(s, "8.0.8", builds)
    assert status == "fail"
    assert "compiles on 7.4.12" in record["failed"][0]
    assert dotnet.judge(s, "7.4.12", builds)[0] == "pass"


def test_a_same_named_type_from_another_package_is_not_drift():
    # An Access Token Management snippet compiled against IdentityServer, whose
    # own `TokenResult` has no `Success`: every version agrees, nothing drifts.
    s = _snip("return TokenResult.Success(token);")
    err = _err("CS0117", "'TokenResult' does not contain a definition for 'Success'")
    surface = _surface(types={"TokenResult"}, members={"TokenResult": {"Token"}})
    builds = {"7.4.12": _compiled(surface, [err]), "8.0.8": _compiled(surface, [err])}
    head = _surface(types={"TokenResult"}, members={"TokenResult": {"Token"}})
    assert dotnet.judge(s, "8.0.8", builds, head)[0] == "unverified"


def test_api_defined_only_at_head_is_reported_as_unreleased():
    s = _snip("builder.Services.AddIdentityServer().AddStorage();")
    err = _err("CS1061", "'IIdentityServerBuilder' does not contain a definition for "
                         "'AddStorage' and no accessible extension method 'AddStorage'")
    rel = _surface(types={"IIdentityServerBuilder"})
    head = _surface(ext={"IIdentityServerBuilder": {"AddStorage"}})
    builds = {"8.0.8": _compiled(rel, [err])}
    status, record = dotnet.judge(s, "8.0.8", builds, head)
    assert status == "fail" and "defined at HEAD" in record["failed"][0]


def test_an_extension_on_another_receiver_is_not_drift():
    s = _snip("var id = context.Request.Client.ClientId;")
    err = _err("CS1061", "'HttpRequest' does not contain a definition for 'Client'")
    head = _surface(ext={"SchemaId": {"Client"}})
    assert dotnet.judge(s, "8.0.8", {"8.0.8": _compiled(_surface(), [err])}, head)[0] \
        == "unverified"


def test_a_call_with_elided_arguments_is_not_judged_on_its_arguments():
    s = _snip("var token = await tools.IssueClientJwtAsync(...);")
    err = _err("CS7036", "There is no argument given that corresponds to the required "
                         "parameter 'ct' of 'IIdentityServerTools.IssueClientJwtAsync(string)'")
    surface = _surface(types={"IIdentityServerTools"})
    builds = {"7.4.12": _compiled(surface), "8.0.8": _compiled(surface, [err])}
    assert dotnet.judge(s, "8.0.8", builds)[0] == "unverified"


def test_two_assemblies_defining_one_type_is_not_drift():
    s = _snip("Show(response);")
    err = _err("CS1503", "Argument 1: cannot convert from 'Duende.IdentityModel.Client."
                         "TokenResponse' to 'Duende.IdentityModel.Client.TokenResponse'")
    surface = _surface(types={"TokenResponse"})
    builds = {"7.4.12": _compiled(surface), "8.0.8": _compiled(surface, [err])}
    assert dotnet.judge(s, "8.0.8", builds)[0] == "unverified"


def test_the_snippets_own_types_are_never_drift():
    s = _snip("public class Client { }\nvar c = new Client().Missing;")
    err = _err("CS1061", "'Client' does not contain a definition for 'Missing'")
    old = _surface(types={"Client"}, members={"Client": {"Missing"}})
    builds = {"7.4.12": _compiled(old), "8.0.8": _compiled(_surface(), [err])}
    assert dotnet.judge(s, "8.0.8", builds)[0] == "unverified"


def test_a_snippet_that_does_not_parse_is_unverified_not_failed():
    s = _snip("services.AddX(options => { ... ")
    syntax = [_err("CS1513", "} expected")]
    status, record = dotnet.judge(s, "8.0.8", {"8.0.8": _compiled(_surface(), syntax=syntax)})
    assert status == "unverified" and "not compiled" in record["notes"][0]


def test_results_block_a_version_that_never_compiled():
    s = _snip("app.UseIdentityServer();")
    builds = [Build("7.4.12", True, compiled=_compiled(_surface())),
              Build("8.0.8", False, "install", "NU1102: Unable to find package")]
    got = {r.version: r for r in dotnet.results([s], builds)}
    assert got["7.4.12"].status == "pass"
    assert got["8.0.8"].status == "blocked" and got["8.0.8"].blocked_by == "install"


def test_harness_output_maps_back_to_snippet_lines():
    s = _snip("using Acme;\nvar x = 1;\nx.Nope();")
    ws = dotnet.workspace([s], "Acme", "1.0.0")
    name, (_, wrapped) = next(iter(ws.by_file.items()))
    file_line = next(k for k, v in wrapped.lines.items() if v == 3)
    raw = json.dumps({"types": ["A"], "members": {"A": ["B"]}, "extensions": {"X": ["Y"]},
                      "namespaces": ["Acme"], "syntax": {},
                      "diagnostics": {name: [{"code": "CS1061", "severity": "error",
                                              "line": file_line, "message": "m"}]}})
    c = dotnet.parse_harness(raw, ws)
    assert c.diags["s1"][0].line == 3
    assert c.surface.extends("X", "Y")
    assert 'Include="Acme" Version="1.0.0"' in ws.files["harness/Harness.csproj"]
