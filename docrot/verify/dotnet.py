""".NET lane: compile every C# snippet against each release of a NuGet package.

C# docs are checked by the compiler rather than by running them. A snippet
is a `Program.cs` excerpt or a class lifted out of a real app; executing it
needs a host, a database and an identity provider, while compiling it needs
only the package. And compiling catches what rots: a type that moved, a
builder method that was renamed, an options property that is gone.

One sandbox per version, and one small program in it - the harness - that
references the package at that version and:

  1. reflects over the package's assemblies: every public namespace, type,
     member and extension method the release ships;
  2. compiles every snippet, each wrapped into a file of its own
     (`extract.csharp.wrap`), with Roslyn, with a `global using` for each of
     those namespaces - docs routinely elide their usings.

A compiler error is only drift when it is about the package. The sandbox
knows nothing about that: it returns raw diagnostics, and `judge` decides on
the host once every version is back, against the union of every surface the
probes saw plus the repo's source at HEAD. `'IIdentityServerBuilder' does not
contain a definition for 'AddAspNetIdentity'` is not drift - that method
ships in a different package - while the same error for a method this
package shipped in 7.0 and dropped in 7.2 is.

Everything else a wrapped excerpt trips over (`_userManager` was a field of
a class the docs never showed) makes the snippet `unverified`, never `fail`.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from xml.sax.saxutils import escape

from ..config import Config, redact
from ..extract import csharp
from ..models import Result, Snippet, Status

IMAGE = "mcr.microsoft.com/dotnet/sdk:10.0"
# Measured: the harness peaks near 700 MB compiling 1,295 snippets. The account
# caps CPU and memory at 10 each, so four of these fit where 4 GiB fit two.
SANDBOX_CPU = 2
SANDBOX_MEMORY_GIB = 2
ACCOUNT_LIMIT = 10
TFM = "net10.0"
ROOT = "/home/daytona/docrot"

# Restore failures worth retrying: NuGet's CDN resets like PyPI's does.
TRANSIENT_RESTORE = re.compile(
    r"(NU1301|timed? ?out|connection reset|temporarily unavailable|503|502|504)", re.I)

# Compiler errors that describe the shape of an API: a type, member or
# namespace that is not there, a call whose arguments no longer fit, an
# override or interface implementation that no longer matches.
MISSING_MEMBER = {"CS1061", "CS0117"}
MISSING_TYPE = {"CS0246"}
MISSING_IN_NAMESPACE = {"CS0234"}
API_SHAPE = MISSING_MEMBER | MISSING_TYPE | MISSING_IN_NAMESPACE | {
    "CS1739", "CS7036", "CS1501", "CS1503",       # arguments
    "CS0029", "CS0266", "CS0200", "CS0122",       # types, setters, visibility
    "CS0115", "CS0535", "CS0534", "CS0508",       # overrides, implementations
    "CS0619", "CS1929", "CS0426",                 # obsolete, receivers, nesting
}
CONVERSION = {"CS1503", "CS0029", "CS0266"}
# Arguments a docs page elided with `...` cannot be counted.
ARGUMENTS = {"CS1739", "CS7036", "CS1501", "CS1503"}
OBSOLETE_WARNING = {"CS0618"}

QUOTED = re.compile(r"'([^']+)'")

# The interfaces ASP.NET's host types are extended through: `app.UseX()` is an
# extension on IApplicationBuilder, called on a WebApplication.
RECEIVES_AS = {
    "WebApplication": ("IApplicationBuilder", "IEndpointRouteBuilder", "IHost"),
    "WebApplicationBuilder": ("IHostApplicationBuilder",),
    "HostApplicationBuilder": ("IHostApplicationBuilder",),
    "ServiceCollection": ("IServiceCollection",),
}


# ------------------------------------------------------------------ workspace

ROSLYN = "4.14.0"
HARNESS_PROJECT = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>{tfm}</TargetFramework>
    <OutputType>Exe</OutputType>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <NuGetAudit>false</NuGetAudit>
    <NoWarn>$(NoWarn);NU1603;NU1608;NU1701</NoWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="{package}" Version="{version}" />
    <PackageReference Include="Microsoft.CodeAnalysis.CSharp" Version="{roslyn}" />
  </ItemGroup>
</Project>
"""

# Runs in the sandbox, next to the package it references. Reflects the
# package's public surface, then compiles every snippet with Roslyn directly.
#
# Not `dotnet build`: the command-line compiler stops after the first phase
# that reports an error, so one snippet with a syntax error - and a docs
# corpus always has one - silently suppressed every semantic error in the
# other 1,294. `Compilation.GetDiagnostics()` reports every phase for every
# file; a snippet that does not parse is set aside on its own.
HARNESS_PROGRAM = r"""
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

var prefix = args[0];
var snipsDir = args[1];
var outPath = args[2];

// ---- 1. the package's public surface, from its own assemblies -------------
// Top-level types only: a nested type is not reachable by its simple name,
// so it can never be what a "type not found" error is about.
var ns = new SortedSet<string>();
var types = new SortedSet<string>();
var members = new SortedDictionary<string, SortedSet<string>>();
var exts = new SortedDictionary<string, SortedSet<string>>();
var assemblies = new List<string>();
var usings = new SortedSet<string>();
var bin = AppContext.BaseDirectory;
// The vendor's other assemblies (Duende.IdentityModel under IdentityServer)
// are dependencies whose namespaces docs also elide. Not for Microsoft/System,
// whose namespaces would make half of every snippet ambiguous.
var vendor = prefix.Split('.')[0];
var shareVendor = vendor != "Microsoft" && vendor != "System";
foreach (var dll in Directory.GetFiles(bin, "*.dll"))
{
    var name = Path.GetFileNameWithoutExtension(dll);
    var own = name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    if (!own && !(shareVendor && name.StartsWith(vendor + ".", StringComparison.OrdinalIgnoreCase)))
        continue;
    Type[] exported;
    try { exported = Assembly.LoadFrom(dll).GetExportedTypes(); }
    catch (ReflectionTypeLoadException e) { exported = e.Types.Where(t => t != null && t.IsPublic).ToArray(); }
    catch { continue; }
    foreach (var t in exported)
        if (t.Namespace != null) usings.Add(t.Namespace);
    if (!own) continue;
    assemblies.Add(name);
    foreach (var t in exported)
    {
        if (t.Namespace != null) ns.Add(t.Namespace);
        if (t.IsNested) continue;
        var tn = t.Name.Split('`')[0];
        types.Add(tn);
        if (!members.TryGetValue(tn, out var set)) members[tn] = set = new SortedSet<string>();
        const BindingFlags all = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static
                               | BindingFlags.FlattenHierarchy;
        MemberInfo[] ms;
        try { ms = t.GetMembers(all); } catch { continue; }
        foreach (var m in ms)
        {
            if (m is ConstructorInfo) continue;
            if (m is MethodInfo mi && mi.IsSpecialName) continue;
            set.Add(m.Name.Split('`')[0]);
            if (m is MethodInfo em && em.IsStatic && em.IsDefined(typeof(ExtensionAttribute), false)
                && em.GetParameters() is { Length: > 0 } ps)
            {
                var receiver = ps[0].ParameterType.Name.Split('`')[0];
                if (!exts.TryGetValue(receiver, out var on)) exts[receiver] = on = new SortedSet<string>();
                on.Add(em.Name);
            }
        }
    }
}

// ---- 2. every snippet, compiled against it --------------------------------
// References: the package and its dependencies as restored, plus the shared
// frameworks this process runs on. Roslyn and this harness are left out so
// their types cannot collide with a snippet's.
var refs = new List<MetadataReference>();
var dirs = new[] {
    Path.GetDirectoryName(typeof(object).Assembly.Location),
    Path.GetDirectoryName(typeof(Microsoft.AspNetCore.Builder.WebApplication).Assembly.Location),
    bin };
var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
foreach (var dir in dirs)
foreach (var dll in Directory.GetFiles(dir!, "*.dll"))
{
    var name = Path.GetFileNameWithoutExtension(dll);
    if (name.StartsWith("Microsoft.CodeAnalysis") || name == "Harness" || !seen.Add(name)) continue;
    try { AssemblyName.GetAssemblyName(dll); } catch { continue; }   // native, not managed
    refs.Add(MetadataReference.CreateFromFile(dll));
}

// The Web SDK's implicit usings, and the namespaces ASP.NET Core docs take
// for granted: a controller's IActionResult, a ClaimsPrincipal, IOptions<T>.
// One the references do not contain is an error in GlobalUsings.cs alone.
var implicitUsings = new[] {
    "System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Net.Http",
    "System.Net.Http.Json", "System.Threading", "System.Threading.Tasks",
    "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting", "Microsoft.AspNetCore.Http",
    "Microsoft.AspNetCore.Routing", "Microsoft.Extensions.Configuration",
    "Microsoft.Extensions.DependencyInjection", "Microsoft.Extensions.Hosting",
    "Microsoft.Extensions.Logging",
    "System.Security.Claims", "System.Text.Json", "System.Diagnostics.CodeAnalysis",
    "Microsoft.AspNetCore.Mvc", "Microsoft.AspNetCore.Mvc.RazorPages",
    "Microsoft.AspNetCore.Authentication", "Microsoft.AspNetCore.Authorization",
    "Microsoft.Extensions.Options", "Microsoft.IdentityModel.Tokens" };
var parse = new CSharpParseOptions(LanguageVersion.Preview);
var globals = CSharpSyntaxTree.ParseText(
    string.Join("\n", implicitUsings.Concat(usings).Distinct().Select(n => $"global using {n};")),
    parse, path: "GlobalUsings.cs");

var trees = new List<SyntaxTree> { globals };
var syntax = new Dictionary<string, List<object>>();
foreach (var file in Directory.GetFiles(snipsDir, "*.cs").OrderBy(f => f))
{
    var tree = CSharpSyntaxTree.ParseText(File.ReadAllText(file), parse, path: Path.GetFileName(file));
    var bad = tree.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error).ToList();
    if (bad.Count > 0) syntax[Path.GetFileName(file)] = bad.Select(Diag).ToList();
    else trees.Add(tree);
}

var options = new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary,
    nullableContextOptions: NullableContextOptions.Disable, allowUnsafe: true,
    concurrentBuild: true);
var compilation = CSharpCompilation.Create("DocRotSnippets", trees, refs, options);
var diags = new Dictionary<string, List<object>>();
foreach (var d in compilation.GetDiagnostics())
{
    if (d.Severity < DiagnosticSeverity.Warning) continue;
    var file = d.Location.SourceTree?.FilePath;
    if (file == null || file == "GlobalUsings.cs") continue;
    if (!diags.TryGetValue(file, out var list)) diags[file] = list = new List<object>();
    list.Add(Diag(d));
}

File.WriteAllText(outPath, JsonSerializer.Serialize(new {
    assemblies, namespaces = ns, types, members, extensions = exts,
    references = refs.Count, compiled = trees.Count - 1, syntax, diagnostics = diags }));
Console.WriteLine($"surface: {types.Count} types; compiled {trees.Count - 1}, {syntax.Count} did not parse");

static object Diag(Diagnostic d) => new {
    code = d.Id, severity = d.Severity == DiagnosticSeverity.Error ? "error" : "warning",
    line = d.Location.GetLineSpan().StartLinePosition.Line + 1, message = d.GetMessage() };
"""


@dataclass
class Workspace:
    """Files for one version's sandbox, and where each snippet went."""
    files: dict[str, str] = field(default_factory=dict)
    by_file: dict[str, tuple[Snippet, csharp.Wrapped]] = field(default_factory=dict)


def workspace(snippets: list[Snippet], package: str, version: str) -> Workspace:
    """The harness for `package` at `version` (`""` for the latest), and every
    snippet wrapped into a file of its own. Snippets live outside the harness
    project, so they are compiled by it rather than into it."""
    ws = Workspace()
    ws.files["harness/Harness.csproj"] = HARNESS_PROJECT.format(
        tfm=TFM, package=escape(package), version=escape(version or "*"), roslyn=ROSLYN)
    ws.files["harness/Program.cs"] = HARNESS_PROGRAM
    for k, s in enumerate(snippets):
        name = f"S{k:04d}.cs"
        w = csharp.wrap(s.code, f"DocRot.Snippets.S{k:04d}")
        ws.files[f"snips/{name}"] = w.source
        ws.by_file[name] = (s, w)
    return ws


def version_arg(version: str) -> str:
    return "" if version.startswith("HEAD") else version


# ------------------------------------------------------------------- parsing

@dataclass
class Diagnostic:
    code: str
    severity: str
    message: str
    line: int                 # line in the snippet, 0 if it fell in the wrapper


@dataclass
class Surface:
    """A package's public API: what the harness reflected from one release,
    or what the repo's source declares at HEAD."""
    namespaces: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)            # top-level, simple names
    members: dict[str, set[str]] = field(default_factory=dict)      # type -> member names
    extensions: dict[str, set[str]] = field(default_factory=dict)   # receiver -> names

    @classmethod
    def of(cls, raw: dict) -> Surface:
        return cls(set(raw.get("namespaces", ())), set(raw.get("types", ())),
                   {t: set(m) for t, m in (raw.get("members") or {}).items()},
                   {t: set(m) for t, m in (raw.get("extensions") or {}).items()})

    def extends(self, receiver: str, name: str) -> bool:
        """Whether `name` is an extension member callable on `receiver`,
        directly or through an interface the ASP.NET host types implement."""
        return any(name in self.extensions.get(r, ())
                   for r in (receiver, *RECEIVES_AS.get(receiver, ())))


@dataclass
class Compiled:
    """What the harness reported for one version."""
    surface: Surface
    diags: dict[str, list[Diagnostic]]         # snippet id -> diagnostics
    syntax: dict[str, list[Diagnostic]]        # snippet id -> parse errors


def parse_harness(raw: str, ws: Workspace) -> Compiled:
    data = json.loads(raw)

    def per_snippet(block: dict) -> dict[str, list[Diagnostic]]:
        out: dict[str, list[Diagnostic]] = {}
        for name, items in (block or {}).items():
            hit = ws.by_file.get(name)
            if not hit:
                continue
            s, w = hit
            out[s.id] = [Diagnostic(code=d["code"], severity=d["severity"],
                                    message=d["message"], line=w.lines.get(d["line"], 0))
                         for d in items]
        return out

    return Compiled(Surface.of(data), per_snippet(data.get("diagnostics")),
                    per_snippet(data.get("syntax")))


def merge(*surfaces: Surface | None) -> Surface:
    out = Surface()
    for s in surfaces:
        if s is None:
            continue
        out.namespaces |= s.namespaces
        out.types |= s.types
        for t, ms in s.members.items():
            out.members.setdefault(t, set()).update(ms)
        for t, ms in s.extensions.items():
            out.extensions.setdefault(t, set()).update(ms)
    return out


# ------------------------------------------------------------------- judging

def _bare(name: str) -> str:
    """`Duende.IdentityServer.Models.Client<T>` -> `Client`."""
    return re.sub(r"<.*$", "", name).rsplit(".", 1)[-1].strip()


def _names(d: Diagnostic) -> list[str]:
    return QUOTED.findall(d.message)


def shipped(d: Diagnostic, s: Surface) -> bool:
    """Does surface `s` ship the name error `d` says is missing?

    Membership is per type: `TokenResult` lacking `Success` only counts if
    some `TokenResult` of this package had a `Success`. By name alone, an
    Access Token Management snippet compiled against IdentityServer - which
    has a `TokenResult` of its own - read as IdentityServer drift.
    """
    q = _names(d)
    if d.code in MISSING_MEMBER and len(q) >= 2:
        receiver, member = _bare(q[0]), _bare(q[1])
        return member in s.members.get(receiver, ()) or s.extends(receiver, member)
    if d.code in MISSING_TYPE and q:
        return _bare(q[0]) in s.types
    if d.code in MISSING_IN_NAMESPACE and len(q) >= 2:
        return f"{q[1]}.{q[0]}" in s.namespaces
    return False


def mentions(d: Diagnostic, s: Surface) -> bool:
    """Does the error quote any type or extension the package owns?"""
    extensions = set().union(*s.extensions.values()) if s.extensions else set()
    for q in _names(d):
        for part in re.split(r"[.(),<> ]+", q):
            if part in s.types or part in extensions:
                return True
    return False


def drift(d: Diagnostic, here: Surface, elsewhere: list[tuple[str, Surface, bool]],
          head: Surface | None, local: set[str], elided: bool) -> str:
    """Why `d` is drift ("" when it is not).

    The snippet, its wrapper, the SDK and the references other than the
    package are identical in every version, so an API-shaped error that one
    version raises and another does not was caused by the package changing -
    provided the name it is about really is the package's in the version that
    compiled. `elsewhere` is (version, its surface, whether that version raised
    the same error).

    An error every version raises is drift only when the repo defines the
    name at HEAD: the docs describe API no release ships yet.
    """
    if d.code not in API_SHAPE or (d.code in ARGUMENTS and elided):
        return ""
    q = _names(d)
    if any(_bare(x) in local for x in q[:2]):
        return ""               # the snippet's own type, not the package's
    if d.code in CONVERSION and len(q) >= 2 and q[-2] == q[-1]:
        return ""               # two assemblies define one name: the references, not the docs

    clean = [(v, s) for v, s, raised in elsewhere if not raised]
    if clean:
        if d.code in MISSING_MEMBER | MISSING_TYPE | MISSING_IN_NAMESPACE:
            ok = [v for v, s in clean if shipped(d, s) and not shipped(d, here)]
        else:
            ok = [v for v, s in clean if mentions(d, s) or mentions(d, here)]
        if ok:
            return "compiles on " + ", ".join(ok)
        return ""

    if head is not None and d.code in MISSING_MEMBER | MISSING_TYPE | MISSING_IN_NAMESPACE \
            and shipped(d, head) and not any(shipped(d, s) for _, s, _ in elsewhere):
        return "defined at HEAD, in no release tested"
    return ""


def _same(a: Diagnostic, b: Diagnostic) -> bool:
    return a.code == b.code and a.line == b.line


def judge(snippet: Snippet, version: str, builds: dict[str, Compiled],
          head: Surface | None = None) -> tuple[Status, dict]:
    """(status, probe record) for one snippet on one version, given what every
    compiled version reported for it.

    A snippet that does not parse was never compiled: its record carries the
    parse errors and it is `unverified`. That is the docs' prose-with-braces
    (`services.AddX(...)` with an elided body), not evidence either way.
    """
    this = builds[version]
    syntax = this.syntax.get(snippet.id)
    if syntax:
        return "unverified", {
            "compiler": [asdict(d) | {"drift": ""} for d in syntax],
            "failed": [], "probe": {},
            "notes": ["does not parse as C# once wrapped - not compiled"]}

    diags = this.diags.get(snippet.id, [])
    errors = [d for d in diags if d.severity == "error"]
    local = {x["name"] for x in snippet.declares}
    elided = csharp.has_elision(snippet.code)
    code_lines = snippet.code.split("\n")

    def show(d: Diagnostic, why: str = "") -> str:
        where = f"line {d.line}" if d.line else "wrapper"
        src = code_lines[d.line - 1].strip() if 0 < d.line <= len(code_lines) else ""
        return (f"{d.code} at {where}: {d.message}" + (f" ({why})" if why else "")
                + (f"\n    {src}" if src else ""))

    reasons: dict[int, str] = {}
    for i, d in enumerate(errors):
        elsewhere = [(v, c.surface, any(_same(d, o) for o in c.diags.get(snippet.id, [])))
                     for v, c in builds.items() if v != version]
        why = drift(d, this.surface, elsewhere, head, local, elided)
        if why:
            reasons[i] = why
    obsolete = [d for d in diags if d.code in OBSOLETE_WARNING and mentions(d, this.surface)]
    other = [d for i, d in enumerate(errors) if i not in reasons]

    record = {
        "compiler": [asdict(d) | {"drift": reasons.get(i, "")} for i, d in enumerate(errors)]
                    + [asdict(d) | {"drift": ""} for d in obsolete],
        "failed": [show(errors[i], why) for i, why in reasons.items()],
        "notes": [f"deprecated: {show(d)}" for d in obsolete]
                 + [f"not judged: {show(d)}" for d in other[:6]],
        "probe": {},
    }
    status: Status
    if reasons:
        status = "fail"
    elif errors:
        status = "unverified"   # the excerpt, not the package, failed to compile
    else:
        status = "pass"
    return status, record


# ------------------------------------------------------------------ sandbox

@dataclass
class Build:
    """What one version's sandbox produced, before judging."""
    version: str
    ok: bool
    blocked_by: str = ""
    note: str = ""
    compiled: Compiled | None = None


async def _new_sandbox(daytona, cfg: Config, log=print):
    from daytona import CreateSandboxFromImageParams, Resources

    from .runner import CREATE_ATTEMPTS, QUOTA_ERROR
    params = CreateSandboxFromImageParams(
        image=cfg.dotnet_image or IMAGE,
        env_vars={"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
                  "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", **cfg.sandbox_env},
        resources=Resources(cpu=SANDBOX_CPU, memory=SANDBOX_MEMORY_GIB, disk=8),
        ephemeral=True,
    )
    last: Exception | None = None
    for attempt in range(1, CREATE_ATTEMPTS + 1):
        try:
            # the first use of an image builds a snapshot from it, which is slow
            return await daytona.create(params, timeout=900,
                                        on_snapshot_create_logs=lambda s: None)
        except Exception as e:
            if QUOTA_ERROR.search(f"{type(e).__name__}: {e}"):
                raise
            last = e
            if attempt < CREATE_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
    raise last if last else RuntimeError("sandbox creation failed")


async def _upload(sandbox, ws: Workspace) -> None:
    from daytona import FileUpload
    await sandbox.fs.upload_files([
        FileUpload(source=text.encode(), destination=f"{ROOT}/{path}")
        for path, text in ws.files.items()])


async def build_version(daytona, snippets: list[Snippet], package: str, prefix: str,
                        version: str, cfg: Config, sem: asyncio.Semaphore,
                        log=print) -> Build:
    """Restore, reflect and compile one version in one sandbox."""
    from .runner import INSTALL_ATTEMPTS, QUOTA_ERROR
    ws = workspace(snippets, package, version_arg(version))
    secrets = cfg.secrets()
    sandbox = None
    try:
        async with sem:
            t0 = time.perf_counter()
            sandbox = await _new_sandbox(daytona, cfg, log)
            await _upload(sandbox, ws)
            t_create = time.perf_counter() - t0

            t0 = time.perf_counter()
            tail = ""
            for attempt in range(1, INSTALL_ATTEMPTS + 1):
                r = await sandbox.process.exec(
                    f"dotnet build -nologo -v q -clp:NoSummary {ROOT}/harness",
                    timeout=cfg.install_timeout)
                if r.exit_code == 0:
                    tail = ""
                    break
                tail = (r.result or "")[-1500:]
                if not TRANSIENT_RESTORE.search(tail):
                    break
                if attempt < INSTALL_ATTEMPTS:
                    await asyncio.sleep(2 * attempt)
            t_install = time.perf_counter() - t0
            if tail:
                log(f"    {version}: install failed")
                return Build(version, False, "install", redact(tail, secrets))

            t0 = time.perf_counter()
            r = await sandbox.process.exec(
                f"dotnet run --no-build --project {ROOT}/harness -- "
                f"{prefix} {ROOT}/snips {ROOT}/out.json", timeout=cfg.build_timeout)
            t_compile = time.perf_counter() - t0
            try:
                raw = await sandbox.fs.download_file(f"{ROOT}/out.json")
                compiled = parse_harness(raw.decode("utf-8", errors="replace"), ws)
            except Exception:
                log(f"    {version}: compile harness failed")
                return Build(version, False, "probe", redact((r.result or "")[-1500:], secrets))
            log(f"    {version}: create {t_create:.1f}s · install {t_install:.1f}s "
                f"· compile {t_compile:.1f}s ({len(compiled.surface.types)} public types, "
                f"{len(compiled.syntax)} snippets did not parse)")
            return Build(version, True, compiled=compiled)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if QUOTA_ERROR.search(msg):
            log(f"    {version}: sandbox quota exceeded - lower --concurrency")
        else:
            log(f"    {version}: sandbox error - {type(e).__name__}")
        return Build(version, False, "sandbox", redact(msg[:800], secrets))
    finally:
        if sandbox is not None:
            try:
                await daytona.delete(sandbox)
            except Exception:
                pass


def results(snippets: list[Snippet], builds: list[Build],
            head: Surface | None = None) -> list[Result]:
    """Judge every snippet on every version, each against all the others."""
    compiled = {b.version: b.compiled for b in builds if b.compiled}
    out: list[Result] = []
    for b in builds:
        for s in snippets:
            if not b.compiled:
                out.append(Result(id=s.id, page=s.page, version=b.version, status="blocked",
                                  blocked_by=b.blocked_by, stderr=b.note, tier=s.tier))
                continue
            status, record = judge(s, b.version, compiled, head)
            out.append(Result(id=s.id, page=s.page, version=b.version, status=status,
                              stderr=json.dumps(record, indent=1), tier=s.tier))
    return out


async def run_dotnet_matrix(snippets: list[Snippet], package: str, prefix: str,
                            versions: list[str], cfg: Config, log=print,
                            head: Surface | None = None) -> list[Result]:
    """The .NET counterpart of `runner.run_matrix`."""
    from daytona import AsyncDaytona

    from .runner import MAX_CONCURRENCY, _cfg, sweep

    code = [s for s in snippets if s.tier != "unverifiable" and csharp.is_csharp(s.lang)]
    rest = [s for s in snippets if s not in code]
    skipped = [Result(id=s.id, page=s.page, version=v, status="unverified", tier=s.tier)
               for s in rest for v in versions]
    if not code:
        return skipped
    if not cfg.can_execute:
        log("  no DAYTONA_API_KEY - skipping compilation, marking unverified")
        return skipped + [Result(id=s.id, page=s.page, version=v, status="unverified",
                                 tier=s.tier) for s in code for v in versions]

    per = max(SANDBOX_CPU, SANDBOX_MEMORY_GIB)
    concurrency = max(1, min(cfg.concurrency, MAX_CONCURRENCY // per, ACCOUNT_LIMIT // per))
    log(f"  {len(versions)} sandboxes ({cfg.dotnet_image or IMAGE}): "
        f"{len(code)} C# snippets compiled per version, concurrency {concurrency}")
    async with AsyncDaytona(_cfg(cfg)) as daytona:
        sem = asyncio.Semaphore(concurrency)
        got = await asyncio.gather(
            *(build_version(daytona, code, package, prefix, v, cfg, sem, log)
              for v in versions), return_exceptions=True)
        builds = []
        for v, b in zip(versions, got, strict=True):
            if isinstance(b, BaseException):
                log(f"    task failed - {type(b).__name__}")
                b = Build(v, False, "sandbox", f"{type(b).__name__}: {b}")
            builds.append(b)
        await sweep(daytona, log)
    return results(code, builds, head) + skipped
