use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Component, Path, PathBuf};
use zed_extension_api::settings::ContextServerSettings;
use zed_extension_api::{
    self as zed, Architecture, Command, ContextServerConfiguration, ContextServerId,
    DownloadedFileType, GithubReleaseAsset, GithubReleaseOptions, Os, Project, Result, serde_json,
};

const CONTEXT_SERVER_ID: &str = "pursers";
const DEFAULT_PACKAGE_SPEC: &str = "pursers-client==0.1.4";
const UV_REPOSITORY: &str = "astral-sh/uv";
const UV_CACHE_DIR: &str = "uv-cache";
const INSTALL_UV_MESSAGE: &str = "As a fallback, install uv from https://docs.astral.sh/uv/ or set uvx_path to its absolute uvx executable, then restart Zed.";

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct PursersSettings {
    central_url: String,
    board_id: String,
    token_file: Option<String>,
    ca_file: Option<String>,
    setup_root: Option<String>,
    uvx_path: Option<String>,
    package_spec: Option<String>,
}

trait UvxProbe {
    fn resolve(&self, configured_path: Option<&str>) -> Result<String>;
}

struct ZedProcessProbe;

impl UvxProbe for ZedProcessProbe {
    fn resolve(&self, configured_path: Option<&str>) -> Result<String> {
        if let Some(path) = configured_path {
            if !is_absolute_path(path) {
                return Err(format!(
                    "Pursers uvx_path must be absolute, but received {path:?}. {INSTALL_UV_MESSAGE}"
                ));
            }
            if probe_uvx(path) {
                println!("Pursers uvx: using configured executable at {path}");
                return Ok(path.to_owned());
            }
            return Err(format!(
                "Pursers could not execute configured uvx_path {path:?}. {INSTALL_UV_MESSAGE}"
            ));
        }

        resolve_unconfigured(&ZedUvSource)
    }
}

trait UvSource {
    fn system_uvx(&self) -> Option<String>;
    fn managed_uvx(&self) -> Result<String>;
}

struct ZedUvSource;

impl UvSource for ZedUvSource {
    fn system_uvx(&self) -> Option<String> {
        locate_uvx()
    }

    fn managed_uvx(&self) -> Result<String> {
        resolve_managed_uvx()
    }
}

fn resolve_unconfigured(source: &impl UvSource) -> Result<String> {
    if let Some(path) = source.system_uvx() {
        println!("Pursers uvx: using system executable at {path}; download skipped");
        return Ok(path);
    }
    source.managed_uvx()
}

fn locate_uvx() -> Option<String> {
    let (shell, args): (&str, &[&str]) = match zed::current_platform().0 {
        Os::Mac | Os::Linux => ("/bin/sh", &["-c", "command -v uvx"]),
        Os::Windows => ("where.exe", &["uvx.exe"]),
    };
    let output = Command::new(shell).args(args.iter().copied()).output();
    match output {
        Ok(output) if output.status == Some(0) => {
            String::from_utf8(output.stdout).ok().and_then(|stdout| {
                stdout
                    .lines()
                    .find(|line| !line.trim().is_empty())
                    .map(str::trim)
                    .map(str::to_owned)
            })
        }
        Ok(_) | Err(_) => None,
    }
    .filter(|path| is_absolute_path(path))
    .filter(|path| probe_uvx(path))
}

fn probe_uvx(path: &str) -> bool {
    matches!(
        Command::new(path).arg("--version").output(),
        Ok(output) if output.status == Some(0)
    )
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ArchiveKind {
    TarGz,
    Zip,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct AssetSpec {
    name: &'static str,
    archive_kind: ArchiveKind,
    executable_name: &'static str,
}

fn asset_for_platform(os: Os, architecture: Architecture) -> Result<AssetSpec> {
    let spec = match (os, architecture) {
        (Os::Mac, Architecture::Aarch64) => AssetSpec {
            name: "uv-aarch64-apple-darwin.tar.gz",
            archive_kind: ArchiveKind::TarGz,
            executable_name: "uvx",
        },
        (Os::Mac, Architecture::X8664) => AssetSpec {
            name: "uv-x86_64-apple-darwin.tar.gz",
            archive_kind: ArchiveKind::TarGz,
            executable_name: "uvx",
        },
        (Os::Linux, Architecture::Aarch64) => AssetSpec {
            name: "uv-aarch64-unknown-linux-gnu.tar.gz",
            archive_kind: ArchiveKind::TarGz,
            executable_name: "uvx",
        },
        (Os::Linux, Architecture::X86) => AssetSpec {
            name: "uv-i686-unknown-linux-gnu.tar.gz",
            archive_kind: ArchiveKind::TarGz,
            executable_name: "uvx",
        },
        (Os::Linux, Architecture::X8664) => AssetSpec {
            name: "uv-x86_64-unknown-linux-gnu.tar.gz",
            archive_kind: ArchiveKind::TarGz,
            executable_name: "uvx",
        },
        (Os::Windows, Architecture::Aarch64) => AssetSpec {
            name: "uv-aarch64-pc-windows-msvc.zip",
            archive_kind: ArchiveKind::Zip,
            executable_name: "uvx.exe",
        },
        (Os::Windows, Architecture::X86) => AssetSpec {
            name: "uv-i686-pc-windows-msvc.zip",
            archive_kind: ArchiveKind::Zip,
            executable_name: "uvx.exe",
        },
        (Os::Windows, Architecture::X8664) => AssetSpec {
            name: "uv-x86_64-pc-windows-msvc.zip",
            archive_kind: ArchiveKind::Zip,
            executable_name: "uvx.exe",
        },
        (Os::Mac, Architecture::X86) => {
            return Err(format!(
                "Pursers cannot download uv for unsupported platform {}. {INSTALL_UV_MESSAGE}",
                platform_name(os, architecture)
            ));
        }
    };
    Ok(spec)
}

fn platform_name(os: Os, architecture: Architecture) -> String {
    let os = match os {
        Os::Mac => "mac",
        Os::Linux => "linux",
        Os::Windows => "windows",
    };
    let architecture = match architecture {
        Architecture::Aarch64 => "aarch64",
        Architecture::X86 => "x86",
        Architecture::X8664 => "x86_64",
    };
    format!("{os}-{architecture}")
}

fn resolve_managed_uvx() -> Result<String> {
    let (os, architecture) = zed::current_platform();
    let platform = platform_name(os, architecture);
    let spec = asset_for_platform(os, architecture)?;
    let cache_dir = PathBuf::from(UV_CACHE_DIR).join(&platform);
    fs::create_dir_all(&cache_dir)
        .map_err(|error| format!("Pursers could not create uv cache {cache_dir:?}: {error}"))?;

    let archive_path = cache_dir.join(spec.name);
    let checksum_path = cache_dir.join(format!("{}.sha256", spec.name));
    let extracted_dir = cache_dir.join("extracted");
    let executable_path = extracted_dir
        .join(archive_root(spec.name))
        .join(spec.executable_name);

    if archive_path.is_file() && checksum_path.is_file() {
        verify_checksum(&archive_path, &checksum_path, spec.name)?;
        if !executable_path.is_file() {
            extract_archive(&archive_path, &extracted_dir, spec.archive_kind)?;
        }
        make_uv_executables(&executable_path)?;
        let absolute = absolute_path(&executable_path)?;
        if probe_uvx(&absolute) {
            println!("Pursers uvx: using verified cached executable at {absolute}");
            return Ok(absolute);
        }
        extract_archive(&archive_path, &extracted_dir, spec.archive_kind)?;
        make_uv_executables(&executable_path)?;
        let absolute = absolute_path(&executable_path)?;
        if probe_uvx(&absolute) {
            println!("Pursers uvx: repaired and verified cached executable at {absolute}");
            return Ok(absolute);
        }
        return Err(format!(
            "Pursers verified cached archive {archive_path:?}, but its uvx executable would not run. {INSTALL_UV_MESSAGE}"
        ));
    }

    let release = zed::latest_github_release(
        UV_REPOSITORY,
        GithubReleaseOptions {
            require_assets: true,
            pre_release: false,
        },
    )
    .map_err(|error| {
        format!(
            "Pursers could not resolve the latest uv release for {platform}: {error}. {INSTALL_UV_MESSAGE}"
        )
    })?;
    let asset_url = find_asset_url(&release.assets, spec.name)?;
    let checksum_name = format!("{}.sha256", spec.name);
    let checksum_url = find_asset_url(&release.assets, &checksum_name)?;

    println!(
        "Pursers uvx: uvx was not found on PATH; downloading uv {} asset {}",
        release.version, spec.name
    );
    download(&checksum_url, &checksum_path)?;
    download(&asset_url, &archive_path)?;
    verify_checksum(&archive_path, &checksum_path, spec.name)?;
    println!("Pursers uvx: checksum verified for {}", spec.name);

    extract_archive(&archive_path, &extracted_dir, spec.archive_kind)?;
    make_uv_executables(&executable_path)?;
    let absolute = absolute_path(&executable_path)?;
    if !probe_uvx(&absolute) {
        return Err(format!(
            "Pursers downloaded and verified {}, but {absolute:?} would not run. {INSTALL_UV_MESSAGE}",
            spec.name
        ));
    }
    println!("Pursers uvx: installed verified executable at {absolute}");
    Ok(absolute)
}

fn find_asset_url(assets: &[GithubReleaseAsset], name: &str) -> Result<String> {
    assets
        .iter()
        .find(|asset| asset.name == name)
        .map(|asset| asset.download_url.clone())
        .ok_or_else(|| {
            format!("Pursers latest uv release has no asset named {name}. {INSTALL_UV_MESSAGE}")
        })
}

fn download(url: &str, path: &Path) -> Result<()> {
    let path_string = path.to_string_lossy().into_owned();
    zed::download_file(url, &path_string, DownloadedFileType::Uncompressed).map_err(|error| {
        format!(
            "Pursers could not download {url} into {path_string:?}: {error}. {INSTALL_UV_MESSAGE}"
        )
    })
}

fn archive_root(name: &str) -> &str {
    name.strip_suffix(".tar.gz")
        .or_else(|| name.strip_suffix(".zip"))
        .unwrap_or(name)
}

fn parse_checksum(contents: &str, asset_name: &str) -> Result<String> {
    let checksum = contents
        .split_whitespace()
        .next()
        .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .ok_or_else(|| format!("Pursers received an invalid checksum file for {asset_name}"))?;
    Ok(checksum.to_ascii_lowercase())
}

fn verify_checksum(archive_path: &Path, checksum_path: &Path, asset_name: &str) -> Result<()> {
    let checksum_contents = fs::read_to_string(checksum_path).map_err(|error| {
        format!("Pursers could not read uv checksum {checksum_path:?}: {error}")
    })?;
    let archive = File::open(archive_path)
        .map_err(|error| format!("Pursers could not read uv archive {archive_path:?}: {error}"))?;
    verify_checksum_reader(archive, &checksum_contents, asset_name).map_err(|error| {
        if error.contains("checksum mismatch") {
            format!("{error} Remove {archive_path:?} and restart Zed to download it again.")
        } else {
            error
        }
    })
}

fn verify_checksum_reader(
    mut archive: impl Read,
    checksum_contents: &str,
    asset_name: &str,
) -> Result<()> {
    let expected = parse_checksum(checksum_contents, asset_name)?;
    let mut hasher = Sha256::new();
    io::copy(&mut archive, &mut hasher)
        .map_err(|error| format!("Pursers could not hash uv archive {asset_name}: {error}"))?;
    let actual = format!("{:x}", hasher.finalize());
    if actual != expected {
        return Err(format!(
            "Pursers refused to run {asset_name}: checksum mismatch (expected {expected}, got {actual})."
        ));
    }
    Ok(())
}

fn extract_archive(archive_path: &Path, destination: &Path, kind: ArchiveKind) -> Result<()> {
    if destination.exists() {
        fs::remove_dir_all(destination).map_err(|error| {
            format!("Pursers could not replace uv directory {destination:?}: {error}")
        })?;
    }
    fs::create_dir_all(destination).map_err(|error| {
        format!("Pursers could not create uv directory {destination:?}: {error}")
    })?;

    match kind {
        ArchiveKind::TarGz => extract_tar_gz(archive_path, destination),
        ArchiveKind::Zip => extract_zip(archive_path, destination),
    }
}

fn extract_tar_gz(archive_path: &Path, destination: &Path) -> Result<()> {
    let archive = File::open(archive_path)
        .map_err(|error| format!("Pursers could not open uv archive {archive_path:?}: {error}"))?;
    let decoder = flate2::read::GzDecoder::new(archive);
    let mut archive = tar::Archive::new(decoder);
    let entries = archive
        .entries()
        .map_err(|error| format!("Pursers could not read uv archive {archive_path:?}: {error}"))?;
    for entry in entries {
        let mut entry = entry.map_err(|error| {
            format!("Pursers could not read an entry in {archive_path:?}: {error}")
        })?;
        let entry_type = entry.header().entry_type();
        if !entry_type.is_file() && !entry_type.is_dir() {
            return Err(format!(
                "Pursers refused unexpected entry type in uv archive {archive_path:?}"
            ));
        }
        let relative = safe_archive_path(&entry.path().map_err(|error| {
            format!("Pursers could not read a path in uv archive {archive_path:?}: {error}")
        })?)
        .ok_or_else(|| format!("Pursers refused an unsafe path in uv archive {archive_path:?}"))?;
        let output = destination.join(relative);
        if entry_type.is_dir() {
            fs::create_dir_all(&output).map_err(|error| {
                format!("Pursers could not create uv directory {output:?}: {error}")
            })?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                format!("Pursers could not create uv directory {parent:?}: {error}")
            })?;
        }
        let mut output_file = File::create(&output)
            .map_err(|error| format!("Pursers could not create uv file {output:?}: {error}"))?;
        io::copy(&mut entry, &mut output_file)
            .map_err(|error| format!("Pursers could not extract uv file {output:?}: {error}"))?;
    }
    Ok(())
}

fn safe_archive_path(path: &Path) -> Option<PathBuf> {
    let mut relative = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => relative.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    (!relative.as_os_str().is_empty()).then_some(relative)
}

fn extract_zip(archive_path: &Path, destination: &Path) -> Result<()> {
    let archive = File::open(archive_path)
        .map_err(|error| format!("Pursers could not open uv archive {archive_path:?}: {error}"))?;
    let mut archive = zip::ZipArchive::new(archive)
        .map_err(|error| format!("Pursers could not read uv archive {archive_path:?}: {error}"))?;
    for index in 0..archive.len() {
        let mut entry = archive.by_index(index).map_err(|error| {
            format!("Pursers could not read an entry in {archive_path:?}: {error}")
        })?;
        let relative = entry.enclosed_name().ok_or_else(|| {
            format!("Pursers refused an unsafe path in uv archive {archive_path:?}")
        })?;
        let output = destination.join(relative);
        if entry.is_dir() {
            fs::create_dir_all(&output).map_err(|error| {
                format!("Pursers could not create uv directory {output:?}: {error}")
            })?;
            continue;
        }
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                format!("Pursers could not create uv directory {parent:?}: {error}")
            })?;
        }
        let mut output_file = File::create(&output)
            .map_err(|error| format!("Pursers could not create uv file {output:?}: {error}"))?;
        io::copy(&mut entry, &mut output_file)
            .map_err(|error| format!("Pursers could not extract uv file {output:?}: {error}"))?;
    }
    Ok(())
}

fn make_executable(path: &Path) -> Result<()> {
    let path = path.to_string_lossy().into_owned();
    zed::make_file_executable(&path).map_err(|error| {
        format!("Pursers could not make downloaded uvx executable at {path:?}: {error}")
    })
}

fn make_uv_executables(uvx_path: &Path) -> Result<()> {
    make_executable(uvx_path)?;
    let uv_path = uvx_path.with_file_name("uv");
    if uv_path.is_file() {
        make_executable(&uv_path)?;
    }
    Ok(())
}

fn absolute_path(path: &Path) -> Result<String> {
    if !path.is_file() {
        return Err(format!(
            "Pursers could not resolve downloaded uvx {path:?}: the extracted file is missing"
        ));
    }
    let absolute = std::env::current_dir()
        .map_err(|error| format!("Pursers could not resolve its extension directory: {error}"))?
        .join(path);
    if !absolute.is_absolute() {
        return Err(format!(
            "Pursers could not resolve downloaded uvx {path:?} to an absolute path"
        ));
    }
    Ok(absolute.to_string_lossy().into_owned())
}

fn is_absolute_path(path: &str) -> bool {
    path.starts_with('/')
        || (path.as_bytes().get(1) == Some(&b':')
            && matches!(path.as_bytes().get(2), Some(b'\\' | b'/')))
}

struct PursersExtension;

impl PursersExtension {
    fn command_for_project(project: &Project) -> Result<Command> {
        let settings = ContextServerSettings::for_project(CONTEXT_SERVER_ID, project)?;
        let value = settings
            .settings
            .ok_or_else(|| "Pursers is not configured. Set central_url and board_id.".to_owned())?;
        let settings = serde_json::from_value(value)
            .map_err(|error| format!("Pursers settings are invalid: {error}"))?;
        build_command(&settings, &ZedProcessProbe)
    }
}

impl zed::Extension for PursersExtension {
    fn new() -> Self {
        Self
    }

    fn context_server_command(
        &mut self,
        context_server_id: &ContextServerId,
        project: &Project,
    ) -> Result<Command> {
        if context_server_id.as_ref() != CONTEXT_SERVER_ID {
            return Err(format!(
                "Pursers received an unknown context server id: {context_server_id}"
            ));
        }
        Self::command_for_project(project)
    }

    fn context_server_configuration(
        &mut self,
        context_server_id: &ContextServerId,
        _project: &Project,
    ) -> Result<Option<ContextServerConfiguration>> {
        if context_server_id.as_ref() != CONTEXT_SERVER_ID {
            return Ok(None);
        }
        Ok(Some(ContextServerConfiguration {
            installation_instructions: include_str!(
                "../configuration/installation_instructions.md"
            )
            .to_owned(),
            settings_schema: include_str!("../configuration/settings_schema.json").to_owned(),
            default_settings: include_str!("../configuration/default_settings.json").to_owned(),
        }))
    }
}

fn build_command(settings: &PursersSettings, probe: &impl UvxProbe) -> Result<Command> {
    require_value("central_url", &settings.central_url)?;
    require_value("board_id", &settings.board_id)?;
    let ca_file = optional_value("ca_file", settings.ca_file.as_deref())?;
    let setup_root = optional_value("setup_root", settings.setup_root.as_deref())?;
    if setup_root.is_some_and(|path| !is_absolute_path(path)) {
        return Err("Pursers setting setup_root must be an absolute path.".to_owned());
    }
    let configured_uvx = optional_value("uvx_path", settings.uvx_path.as_deref())?;
    let token_file = optional_value("token_file", settings.token_file.as_deref())?;
    let package_spec = optional_value("package_spec", settings.package_spec.as_deref())?
        .unwrap_or(DEFAULT_PACKAGE_SPEC);

    let uvx = probe.resolve(configured_uvx)?;
    if !is_absolute_path(&uvx) {
        return Err(INSTALL_UV_MESSAGE.to_owned());
    }
    println!("Pursers resolved uvx executable: {uvx}");

    let mut args = vec![
        "--from".to_owned(),
        package_spec.to_owned(),
        "pursers-mcp".to_owned(),
        "--central-url".to_owned(),
        settings.central_url.to_owned(),
        "--board".to_owned(),
        settings.board_id.to_owned(),
    ];
    if let Some(token_file) = token_file {
        args.extend(["--token-file".to_owned(), token_file.to_owned()]);
    }
    if let Some(ca_file) = ca_file {
        args.extend(["--ca-file".to_owned(), ca_file.to_owned()]);
    }
    if let Some(setup_root) = setup_root {
        args.extend(["--setup-root".to_owned(), setup_root.to_owned()]);
    }

    Ok(Command {
        command: uvx,
        args,
        env: Vec::new(),
    })
}

fn require_value<'a>(name: &str, value: &'a str) -> Result<&'a str> {
    if value.trim().is_empty() {
        return Err(format!("Pursers setting {name} cannot be empty."));
    }
    Ok(value)
}

fn optional_value<'a>(name: &str, value: Option<&'a str>) -> Result<Option<&'a str>> {
    value.map(|value| require_value(name, value)).transpose()
}

zed::register_extension!(PursersExtension);

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::io::Cursor;

    struct Available;

    impl UvxProbe for Available {
        fn resolve(&self, configured_path: Option<&str>) -> Result<String> {
            Ok(configured_path
                .unwrap_or("/opt/homebrew/bin/uvx")
                .to_owned())
        }
    }

    struct Missing;

    impl UvxProbe for Missing {
        fn resolve(&self, _configured_path: Option<&str>) -> Result<String> {
            Err(INSTALL_UV_MESSAGE.to_owned())
        }
    }

    struct BareName;

    impl UvxProbe for BareName {
        fn resolve(&self, _configured_path: Option<&str>) -> Result<String> {
            Ok("uvx".to_owned())
        }
    }

    fn settings() -> PursersSettings {
        PursersSettings {
            central_url: "https://central.example.test/mcp".to_owned(),
            board_id: "example-board".to_owned(),
            token_file: Some("/credentials/worker.jwt".to_owned()),
            ca_file: Some("/credentials/ca.pem".to_owned()),
            setup_root: None,
            uvx_path: None,
            package_spec: None,
        }
    }

    #[test]
    fn settings_should_build_exact_contract_arguments() {
        let command = build_command(&settings(), &Available).expect("valid command");

        assert_eq!(command.command, "/opt/homebrew/bin/uvx");
        assert_ne!(command.command, "uvx");
        assert_eq!(
            command.args,
            [
                "--from",
                DEFAULT_PACKAGE_SPEC,
                "pursers-mcp",
                "--central-url",
                "https://central.example.test/mcp",
                "--board",
                "example-board",
                "--token-file",
                "/credentials/worker.jwt",
                "--ca-file",
                "/credentials/ca.pem",
            ]
        );
    }

    #[test]
    fn missing_uvx_should_return_install_instructions() {
        let error = build_command(&settings(), &Missing).expect_err("uvx must be required");

        assert_eq!(error, INSTALL_UV_MESSAGE);
    }

    #[test]
    fn bare_name_uvx_should_never_be_returned_in_the_command() {
        let error = build_command(&settings(), &BareName).expect_err("uvx must be absolute");

        assert_eq!(error, INSTALL_UV_MESSAGE);
    }

    #[test]
    fn empty_token_file_should_be_rejected() {
        let mut settings = settings();
        settings.token_file = Some("  ".to_owned());

        let error = build_command(&settings, &Available).expect_err("token file is required");

        assert_eq!(error, "Pursers setting token_file cannot be empty.");
    }

    #[test]
    fn relative_setup_root_should_be_rejected_before_command_launch() {
        let mut settings = settings();
        settings.setup_root = Some("relative-central".to_owned());

        let error = build_command(&settings, &Available).expect_err("setup root must be absolute");

        assert_eq!(
            error,
            "Pursers setting setup_root must be an absolute path."
        );
    }

    #[test]
    fn package_spec_and_uvx_path_should_override_defaults() {
        let mut settings = settings();
        settings.package_spec = Some("/workspace/pursers/packages/client".to_owned());
        settings.uvx_path = Some("/opt/uv/bin/uvx".to_owned());
        settings.setup_root = Some("/workspace/local-central".to_owned());

        let command = build_command(&settings, &Available).expect("valid overrides");

        assert_eq!(command.command, "/opt/uv/bin/uvx");
        assert_eq!(command.args[1], "/workspace/pursers/packages/client");
        assert!(
            command
                .args
                .windows(2)
                .any(|args| args == ["--setup-root", "/workspace/local-central"])
        );
    }

    #[test]
    fn explicit_uvx_path_should_be_passed_to_the_probe_unchanged() {
        use std::cell::RefCell;

        struct RecordingProbe<'a>(&'a RefCell<Vec<Option<String>>>);

        impl UvxProbe for RecordingProbe<'_> {
            fn resolve(&self, configured_path: Option<&str>) -> Result<String> {
                self.0.borrow_mut().push(configured_path.map(str::to_owned));
                Ok(configured_path.expect("configured path").to_owned())
            }
        }

        let calls = RefCell::new(Vec::new());
        let mut settings = settings();
        settings.uvx_path = Some("/custom path/bin/uvx".to_owned());

        let command = build_command(&settings, &RecordingProbe(&calls)).expect("valid command");

        assert_eq!(command.command, "/custom path/bin/uvx");
        assert_eq!(
            calls.into_inner(),
            [Some("/custom path/bin/uvx".to_owned())]
        );
    }

    #[test]
    fn absolute_path_validation_should_reject_bare_names() {
        assert!(is_absolute_path("/opt/homebrew/bin/uvx"));
        assert!(is_absolute_path(r"C:\Tools\uvx.exe"));
        assert!(!is_absolute_path("uvx"));
        assert!(!is_absolute_path("bin/uvx"));
    }

    #[test]
    fn local_defaults_should_not_require_or_pass_a_token_path() {
        let mut settings = settings();
        settings.token_file = None;

        let command = build_command(&settings, &Available).expect("valid local defaults");

        assert!(!command.args.iter().any(|value| value == "--token-file"));
        assert!(!command.args.iter().any(|value| value.contains("/PATH/TO/")));
    }

    #[test]
    fn existing_uvx_should_be_preferred_without_downloading() {
        struct Existing<'a>(&'a Cell<usize>);

        impl UvSource for Existing<'_> {
            fn system_uvx(&self) -> Option<String> {
                Some("/opt/homebrew/bin/uvx".to_owned())
            }

            fn managed_uvx(&self) -> Result<String> {
                self.0.set(self.0.get() + 1);
                Ok("/managed/uvx".to_owned())
            }
        }

        let download_calls = Cell::new(0);
        let resolved = resolve_unconfigured(&Existing(&download_calls)).expect("system uvx");

        assert_eq!(resolved, "/opt/homebrew/bin/uvx");
        assert_eq!(download_calls.get(), 0);
    }

    #[test]
    fn checksum_mismatch_should_refuse_the_archive() {
        let error = verify_checksum_reader(
            Cursor::new(b"not the published archive"),
            "0000000000000000000000000000000000000000000000000000000000000000  uv.tar.gz\n",
            "uv.tar.gz",
        )
        .expect_err("mismatched archive must be rejected");

        assert!(error.contains("refused to run uv.tar.gz: checksum mismatch"));
    }

    #[test]
    fn matching_checksum_should_accept_the_archive() {
        let bytes = b"published archive";
        let checksum = format!("{:x}  uv.tar.gz\n", Sha256::digest(bytes));

        verify_checksum_reader(Cursor::new(bytes), &checksum, "uv.tar.gz")
            .expect("matching archive");
    }

    #[test]
    fn archive_paths_should_stay_inside_the_private_directory() {
        assert_eq!(
            safe_archive_path(Path::new("uv-platform/uvx")),
            Some(PathBuf::from("uv-platform/uvx"))
        );
        assert_eq!(safe_archive_path(Path::new("../uvx")), None);
        assert_eq!(safe_archive_path(Path::new("/uvx")), None);
    }

    #[test]
    fn unsupported_platform_should_be_named_in_the_error() {
        let error = asset_for_platform(Os::Mac, Architecture::X86)
            .expect_err("32-bit macOS has no uv release asset");

        assert!(error.contains("unsupported platform mac-x86"));
    }

    #[test]
    fn supported_platforms_should_map_to_published_asset_names() {
        assert_eq!(
            asset_for_platform(Os::Mac, Architecture::Aarch64)
                .expect("Apple Silicon")
                .name,
            "uv-aarch64-apple-darwin.tar.gz"
        );
        assert_eq!(
            asset_for_platform(Os::Linux, Architecture::X8664)
                .expect("x64 Linux")
                .name,
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        );
        assert_eq!(
            asset_for_platform(Os::Windows, Architecture::X86)
                .expect("x86 Windows")
                .name,
            "uv-i686-pc-windows-msvc.zip"
        );
    }

    #[test]
    fn schema_and_defaults_should_form_a_strict_valid_configuration() {
        let schema: serde_json::Value =
            serde_json::from_str(include_str!("../configuration/settings_schema.json"))
                .expect("valid schema JSON");
        let defaults: PursersSettings =
            serde_json::from_str(include_str!("../configuration/default_settings.json"))
                .expect("defaults match settings");

        assert_eq!(schema["additionalProperties"], false);
        assert!(
            !schema["required"]
                .as_array()
                .expect("required array")
                .iter()
                .any(|value| value == "token_file")
        );
        assert!(
            !schema["required"]
                .as_array()
                .expect("required array")
                .iter()
                .any(|value| value == "setup_root")
        );
        assert_eq!(defaults.board_id, "pursers-local");
        assert_eq!(defaults.token_file, None);
        assert!(!include_str!("../configuration/default_settings.json").contains("/PATH/TO/"));
    }

    #[test]
    fn unknown_setting_should_be_rejected() {
        let error = serde_json::from_str::<PursersSettings>(
            r#"{
                "central_url":"http://127.0.0.1:8766/mcp",
                "board_id":"pursers-local",
                "token_file":"/PATH/TO/PURSERS/worker.jwt",
                "token":"must-not-be-here"
            }"#,
        )
        .expect_err("unknown settings must fail");

        assert!(error.to_string().contains("unknown field `token`"));
    }
}
