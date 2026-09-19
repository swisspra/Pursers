use serde::Deserialize;
use zed_extension_api::settings::ContextServerSettings;
use zed_extension_api::{
    self as zed, Command, ContextServerConfiguration, ContextServerId, Project, Result, serde_json,
};

const CONTEXT_SERVER_ID: &str = "pursers";
const DEFAULT_PACKAGE_SPEC: &str = "pursers-client==0.1.0";
const INSTALL_UV_MESSAGE: &str = "Pursers could not start uvx. Install uv: https://docs.astral.sh/uv/ then restart Zed. You can also set uvx_path to the uvx executable.";

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct PursersSettings {
    central_url: String,
    board_id: String,
    token_file: String,
    ca_file: Option<String>,
    uvx_path: Option<String>,
    package_spec: Option<String>,
}

trait UvxProbe {
    fn verify(&self, command: &str) -> Result<()>;
}

struct ZedProcessProbe;

impl UvxProbe for ZedProcessProbe {
    fn verify(&self, command: &str) -> Result<()> {
        let output = Command::new(command).arg("--version").output();
        match output {
            Ok(output) if output.status == Some(0) => Ok(()),
            Ok(_) | Err(_) => Err(INSTALL_UV_MESSAGE.to_owned()),
        }
    }
}

struct PursersExtension;

impl PursersExtension {
    fn command_for_project(project: &Project) -> Result<Command> {
        let settings = ContextServerSettings::for_project(CONTEXT_SERVER_ID, project)?;
        let value = settings.settings.ok_or_else(|| {
            "Pursers is not configured. Set central_url, board_id, and token_file.".to_owned()
        })?;
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
    require_value("token_file", &settings.token_file)?;

    let ca_file = optional_value("ca_file", settings.ca_file.as_deref())?;
    let uvx = optional_value("uvx_path", settings.uvx_path.as_deref())?.unwrap_or("uvx");
    let package_spec = optional_value("package_spec", settings.package_spec.as_deref())?
        .unwrap_or(DEFAULT_PACKAGE_SPEC);

    probe.verify(uvx)?;

    let mut args = vec![
        "--from".to_owned(),
        package_spec.to_owned(),
        "pursers-mcp".to_owned(),
        "--central-url".to_owned(),
        settings.central_url.to_owned(),
        "--board".to_owned(),
        settings.board_id.to_owned(),
        "--token-file".to_owned(),
        settings.token_file.to_owned(),
    ];
    if let Some(ca_file) = ca_file {
        args.extend(["--ca-file".to_owned(), ca_file.to_owned()]);
    }

    Ok(Command {
        command: uvx.to_owned(),
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

    struct Available;

    impl UvxProbe for Available {
        fn verify(&self, _command: &str) -> Result<()> {
            Ok(())
        }
    }

    struct Missing;

    impl UvxProbe for Missing {
        fn verify(&self, _command: &str) -> Result<()> {
            Err(INSTALL_UV_MESSAGE.to_owned())
        }
    }

    fn settings() -> PursersSettings {
        PursersSettings {
            central_url: "https://central.example.test/mcp".to_owned(),
            board_id: "example-board".to_owned(),
            token_file: "/credentials/worker.jwt".to_owned(),
            ca_file: Some("/credentials/ca.pem".to_owned()),
            uvx_path: None,
            package_spec: None,
        }
    }

    #[test]
    fn settings_should_build_exact_contract_arguments() {
        let command = build_command(&settings(), &Available).expect("valid command");

        assert_eq!(
            command.args,
            [
                "--from",
                "pursers-client==0.1.0",
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
    fn empty_token_file_should_be_rejected() {
        let mut settings = settings();
        settings.token_file = "  ".to_owned();

        let error = build_command(&settings, &Available).expect_err("token file is required");

        assert_eq!(error, "Pursers setting token_file cannot be empty.");
    }

    #[test]
    fn package_spec_and_uvx_path_should_override_defaults() {
        let mut settings = settings();
        settings.package_spec = Some("/workspace/pursers/packages/client".to_owned());
        settings.uvx_path = Some("/opt/uv/bin/uvx".to_owned());

        let command = build_command(&settings, &Available).expect("valid overrides");

        assert_eq!(command.command, "/opt/uv/bin/uvx");
        assert_eq!(command.args[1], "/workspace/pursers/packages/client");
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
        assert_eq!(defaults.board_id, "pursers-local");
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
