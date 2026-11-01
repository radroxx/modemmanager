# SMS Receive Specification

## Purpose

TBD — update Purpose after archive.

## Requirements

### Requirement: Periodic backup poll of message storage

The system SHALL periodically poll the modem's message storage and process any messages found there the same way it processes a message delivered via notification, regardless of whether the unsolicited `+CMTI` notification arrived. The poll interval SHALL be configurable.

#### Scenario: Message arrives without a notification

- **WHEN** a message appears in the modem's storage but no notification of its arrival is received
- **THEN** the system detects and processes this message no later than the next backup poll: it reads, deletes from storage, assembles (if part of a multipart message), and publishes it the same way as a message received via notification

#### Scenario: Notification arrives normally

- **WHEN** a message has already been processed via the `+CMTI` notification before the backup poll occurs
- **THEN** the backup poll does not find this message in storage and does not create a duplicate event

#### Scenario: Backup poll finds unprocessed messages

- **WHEN** the backup poll discovers at least one message in storage
- **THEN** the system records in the journal that the message was received without a preceding notification, so that notification unreliability on a specific modem is visible during diagnostics
