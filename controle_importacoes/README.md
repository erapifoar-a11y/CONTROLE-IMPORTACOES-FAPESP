# Controle de Importações FAPESP 0.2.2

Aplicativo local para acompanhar importações vinculadas a processos FAPESP.

## Novidades da versão 0.2.0

- editar, reativar ou excluir importações;
- editar a classificação e a descrição dos documentos ou excluir anexos indevidos;
- os botões Cancelar e X fecham os formulários sem exigir preenchimento;
- sugestões clicáveis em pesquisador, processo, exportador, fabricante e representante;
- busca instantânea também por fabricante e representante;
- carga única do acervo histórico sem substituir o banco existente;
- registros históricos concluídos e marcados como **Conferir**, para correção posterior.

## Correção da versão 0.2.1

- **Processos** apresenta os registros agrupados por processo, com os totais por moeda;
- **Importações** apresenta uma lista geral, com uma importação por linha e filtros próprios.

## Correção da versão 0.2.2

- o acervo histórico deixou de ser incorporado ao executável e ao repositório;
- em **Configurações**, o usuário seleciona o arquivo original `IMPORTAÇÕES.zip` para realizar a carga;
- o pacote destinado ao GitHub permanece pequeno e dentro dos limites de envio.

## Recursos desta versão

- autenticação local com senha;
- cadastro digitável de pesquisador;
- processo FAPESP obrigatório;
- criação de importações;
- valores totalizados exclusivamente por processo e moeda;
- distinção entre valores em preparação e autorizados/concluídos;
- anexação e versionamento básico de documentos;
- categorias para proforma vencedora e propostas de cobertura;
- união exclusiva das propostas de cobertura em PDF, sem incluir a vencedora;
- histórico de eventos;
- situações operacionais;
- backup e restauração segura do banco e dos anexos;
- identidade visual cobre/laranja queimado.

## Executar em desenvolvimento

```bash
python -m pip install -r requirements.txt
python run.py
```

Com `pywebview` instalado, abre como janela desktop. Sem ele, abre no navegador local. Os dados permanecem na pasta `data` e o aplicativo não requer internet.

No Windows, também é possível executar `INICIAR.bat`. Na primeira utilização ele cria o ambiente e instala as dependências.

## Estrutura dos dados

- `data/controle_importacoes.db`: banco SQLite;
- `data/documentos/`: anexos organizados internamente por importação;
- `data/backups/`: backups gerados pelo aplicativo.

## Observações

Esta é a versão inicial para homologação do fluxo. Diligências detalhadas, relatórios PDF/Excel e empacotamento definitivo para Windows serão refinados após os primeiros testes.

## Gerar o executável do Windows

O projeto inclui `Controle_Importacoes.spec` e um fluxo de compilação em `.github/workflows/build-windows.yml`. O executável armazena os dados em `%LOCALAPPDATA%\ControleImportacoesFAPESP`, fora da pasta temporária do programa.
