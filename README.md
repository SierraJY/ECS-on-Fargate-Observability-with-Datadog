# ECS on Fargate 호스트 제어권 공백 대응 모니터링&관측성 아키텍처 with Datadog

## 1. 개요

### 1.1 배경 및 목적

- `Amazon ECS` on `AWS Fargate`는 호스트 OS와 커널에 대한 사용자 접근을 원천적으로 차단하는 서버리스 컴퓨팅 환경이다
- 이 환경에서는 EC2 기반 인프라에서 당연하게 여겨지던 "호스트에 모니터링 에이전트를 설치한다"는 접근 방식이 성립하지 않는다
- 본 프로젝트는 이러한 제약 하에서 인프라, 네트워크, 로그, APM 데이터를 유실 없이 수집하여 Datadog으로 통합하는 방법과 아키텍처를 정리하는 것을 목적으로 한다

### 1.2 범위

- `ECS on Fargate` 환경의 구조적 특성과 이로부터 파생되는 관측성 제약
- `AWS FireLens`, `Datadog Agent`, `ECS Action Log`, `CloudWatch`, `ECS Service Event`를 통한 인프라·네트워크·로그·APM 데이터 수집 방법
- FastAPI 기반 MSA 데모 환경을 통한 검증
- 프론트엔드(RUM), DB 쿼리 레벨(DBM)까지 관측 범위 확장
    - ECS & Fargate 제약의 연장은 아님

## 2. ECS on Fargate 환경 설명

### 2.1 ECS & Fargate 정의

- ECS는 컨테이너화된 애플리케이션의 배포와 스케일링을 관리하는 오케스트레이션 서비스
- 컨테이너를 실행할 컴퓨팅 자원으로 `EC2 launch type`과 `Fargate launch type` 중 하나를 선택할 수 있다
- `Fargate`는 컨테이너에 필요한 vCPU·메모리를 태스크 단위로 동적 할당하는 서버리스 컴퓨팅 엔진
- 사용자는 기반 EC2 인스턴스를 프로비저닝하거나 관리하지 않으며, 해당 인스턴스의 OS·커널에 접근할 수 있는 수단(SSH, SSM 세션 등)이 제공되지 않는다
- 이 "호스트/커널 접근 완전 격리"라는 특성이 대부분의 관측성 제약의 공통 근원

### 2.2 ECS on Fargate 네트워킹 구조

- `awsvpc`네트워크 모드
    - ECS Task마다 독립된 ENI와 고유 사설 IP를 할당하는 네트워크 모드
    - Fargate에서는 이 모드만 지원되며(`bridge`, `host` 모드는 EC2 launch type 전용), 사실상 강제
    - 같은 Task 내에 정의된 여러 컨테이너는 이 ENI를 공유하므로 localhost로 서로 통신할 수 있다
    - 이는 `Datadog Agent 사이드카` 패턴, dd-trace의 트레이스 전송이 성립하는 전제 조건
- `ECS Service Connect`
    - ECS Service Connect는 ECS Task/Service 간 서비스 디스커버리와 트래픽 관리를 제공하는 AWS 관리형 기능이다
    - ECS 환경에서의`Service Mesh`로 간주할 수 있다
        - K8S/EKS 환경의 istio와 개념적으로 대응
        - ECS에서 istio는 정상적으로 사용하기엔 어려움
    - 활성화하면 `Envoy`기반 프록시 컨테이너가 Task 내에 자동으로 주입되어, 애플리케이션 코드 수정 없이 서비스 간 통신을 중계한다
    - AWS App Mesh가 서비스 종료 예정이되면서, ECS 환경에서 서비스 메시가 필요한 경우 Service Connect가 사실상 표준 선택지

## 3. 관측성 관점의 태생적 제약사항

### 3.1 호스트 접근 불가로 인한 제약

- 호스트/커널 접근 불가로 인한 제약
- 2.1에서 설명한 대로 Fargate는 호스트 OS·커널에 대한 사용자 접근을 제공하지 않는다
    - ECS Exec로 가능할지라도, 환경 설정 시 ECS Exec를 지양하는 것이 좋은 접근법
- 이 하나의 특성이 세 갈래의 관측성 제약을 만든다
    - 인프라 메트릭 수집 제약
        - 전통적으로 CPU/메모리/디스크 메트릭은 호스트에 설치된 에이전트가 `/proc`, cgroup 등을 직접 읽어 수집한다
        - Fargate는 이 설치 자체가 불가능하다. → 4.1에서 해결
    - 로그 수집 제약
        - 호스트의 로그 파일(`/var/log/...`)에 접근하거나 호스트 단위로 로그 수집 에이전트를 설치하는 방식이 불가능하다. → 4.3에서 해결
    - 커널 레벨 네트워크 모니터링 제약
        - Datadog Cloud Network Monitoring(CNM)은 eBPF 기반 system-probe로 커널 레벨에서 패킷·커넥션을 관찰하는데, 이 역시 커널 접근을 전제로 한다
        - ECS Fargate에서 CNM은 Preview 단계이며 Datadog 담당자에게 별도 신청해야 활성화 가능하다 (self-service 불가)
        - CNM 자체는 채택 불가한 경우 문제가 발생한다 → 4.2.1에서 대안(정확히는 "보완책": CNM 자체의 대체가 아니라 별도 관측 지점 확보) 제시

### 3.2 Service Connect 프록시(Envoy) 내부 관측 제약

- 2.3에서 설명한 Envoy 기반 프록시는 Task 내 모든 서비스 간 트래픽을 중계하므로, 이론적으로는 마이크로서비스 간 RPS·에러율·레이턴시·커넥션 상태를 관측할 수 있는 최적의 지점이다
- 그러나 Service Connect의 Envoy는 완전 관리형(Managed Envoy)이라, App Mesh나 Kubernetes/Istio처럼 사용자가 Envoy bootstrap 설정·admin 인터페이스 노출 여부를 직접 제어할 수 없다
- **메트릭**: 표준적인 Envoy 관측 방법(admin/stats 엔드포인트 노출)이 원천적으로 불가능하다 — "기본값이 꺼져 있어서 켜야 하는" 문제가 아니라 "사용자가 켤 수 있는 수단 자체가 없는" 문제다 → AWS가 큐레이션해서 외부로 노출하는 대체 지점(CloudWatch 네이티브 메트릭)으로 4.2.2에서 해결
- **로그**: 메트릭과 달리 AWS가 opt-in 기능(Envoy Access Log)을 공식 제공하므로 완전히 막혀있는 제약은 아니다 → 4.2.3에서 해결(단 client-only 모드의 발신 로그 미기록이라는 별도 공백 존재)
- 즉 3.1은 "커널에 접근할 수단이 없다"는 제약, 3.2의 메트릭은 "Envoy 내부에 접근할 수단이 없다"는 같은 층위의 제약이지만, 로그는 AWS가 예외적으로 열어둔 경로가 있다는 점에서 성격이 다르다

### 3.3 ECS 컨트롤 플레인 자체의 불투명성

- 3.1/3.2가 각각 "호스트에 접근할 수단이 없다", "Envoy 내부에 접근할 수단이 없다"는 제약이라면, 이건 한 단계 더 위 계층의 같은 성격 문제다
- ECS가 배포·서비스 업데이트·managed daemon 갱신 과정에서 사용자를 대신해 내부적으로 수행하는 판단과 작업 자체가 불투명하다
- 정확히는 Fargate 고유 제약이 아니라 ECS 자체의 제약이다(EC2 launch type ECS에도 동일하게 적용됨)
- 그럼에도 3장에 포함시키는 이유는, "AWS 관리형 서비스가 특정 레이어를 완전히 추상화하면서 생기는 구조적 공백"이라는 3.1/3.2와 동일한 패턴이기 때문이다
    - 4.6/4.7의 RUM·DBM처럼 제약과 무관하게 관측 범위 자체를 넓히는 것과는 다름
- 지금까지는 배포가 이상하게 동작해도 ECS 내부에서 무슨 판단을 했는지 알 방법이 없어 AWS Support 문의나 여러 로그를 짜맞추는 추론이 필요했다 → 4.5에서 해결

## 4. 관측성 확보 방안

### 4.1 인프라 모니터링

- Task Definition 내에 Datadog Agent 컨테이너를 사이드카로 추가한다
- 필요 설정
    - `ECS_FARGATE=true`환경 변수, API Key, 필요 IAM 권한
- 수집 원리
    - 전통적인 CPU/메모리/디스크 메트릭 수집은 호스트에 설치된 Agent가 `/proc`, cgroup을 직접 읽는 방식인데, Fargate는 호스트 자체를 추상화하므로 이 방식이 성립하지 않는다
    - 사이드카로 뜬 Datadog Agent도 동일한 제약을 받는 컨테이너일 뿐이라 예외가 아니다
    - Fargate는 이를 우회하기 위해 태스크 내부에서만 접근 가능한 Task Metadata Endpoint를 제공한다(`ECS_CONTAINER_METADATA_URI_V4`환경 변수로 각 컨테이너에 자동 주입)
    - 이 엔드포인트는 같은 태스크 네트워크 네임스페이스 내부에서만 도달 가능하다
    - 모니터링하려는 태스크마다 Datadog Agent를 사이드카로 개별 배치해야 하는 이유(외부에서 대신 긁어올 방법이 없음)
    - `ECS_FARGATE=true`가 이 폴링 방식을 활성화하는 스위치이며, Agent는 주기적으로 이 API를 호출해 태스크 내 각 컨테이너 단위 지표를 가져와 Datadog으로 전송
- 수집 데이터
    - 개별 컨테이너 단위 CPU/메모리 사용량, 네트워크 I/O(Fargate 플랫폼 버전 1.4.0 이상에서 제공되는 태스크 단위 네트워크 성능 메트릭 포함)
- 참고
    - 4.1에서 다루는 Datadog Agent 사이드카가 이미 Task 내에 떠 있다는 이유로 "그 Agent가 로그도 같이 수집하면 되지 않나"라고 생각할 수 있으나, 이는 성립하지 않는다
    - Datadog Agent의 표준 로그 수집 방식은 Docker 소켓(`/var/run/docker.sock`)과 호스트의 컨테이너 로그 파일(`/var/lib/docker/containers/...`)을 직접 읽는 방식인데, 이 역시 호스트 접근을 전제로 하므로 Fargate에서는 Agent 자신도 동일한 제약을 받는다
    - 컨테이너의 stdout/stderr는 컨테이너 런타임이 가로채 지정된 로그 드라이버로 전달하는 구조이며, 이 전달 경로에 개입하려면 `awsfirelens` 같은 로그 드라이버로 명시적으로 라우팅해야 한다
    - Datadog Agent는 이 로그 드라이버 역할을 하지 않으므로, 사이드카로 떠 있는 것만으로는 다른 컨테이너의 로그를 수집할 수 없다

### 4.2 네트워크 모니터링

#### 4.2.1 커널 레벨 네트워크 제약 해결

- 3.1의 커널 레벨 제약 및 CNM 사용불가 대응
- CNM은 Preview·별도 신청 필요라 채택 불가로 확정, self-service 대안으로 VPC Flow Logs 채택
- Datadog 연동
    - VPC Flow Logs → Kinesis Data Firehose(직접 전송) → Datadog HTTP endpoint
    - ENI 단위 flow record 수신 확인
- 한계
    - 로그 기반 수 분 단위 지연, DNS 쿼리 레벨 상세 없음, 프로세스 단위 귀속 불가(ENI 단위까지만 구분)

#### 4.2.2 Service Connect 프록시 메트릭 수집

- Service Connect의 Envoy는 완전 관리형(Managed)이라 사용자가 Envoy 설정에 개입할 수 없음
- CloudWatch Metric Streams(Kinesis Data Firehose 경유) 채택
    - 폴링 방식(AWS Integration) 대신 실시간에 가까운(2~3분) 스트리밍 방식
- 확인할 지표
    - `RequestCount`
    - `RequestCountPerTarget`
    - `HTTPCode_Target_2XX/4XX/5XX_Count`
    - `TargetResponseTime`
    - `ActiveConnectionCount`
    - `NewConnectionCount`
- Metric Streams를 쓰더라도 Datadog에 해당 AWS 계정이 연동(Integrations → AWS)되어 있어야 지표가 인식됨
    - 로그(Flow Logs)와 달리 계정 연동이 선행 조건
    - Datadog AWS Integration의 "Metric Collection" on/off 토글은 API 폴링에만 영향을 주고 Metric Streams 자체는 AWS 쪽 리소스를 직접 삭제/수정해야만 끌 수 있음

#### 4.2.3 Service Connect 프록시 로그 (Envoy Access Log)

- 3.2의 "Envoy 완전 관리형" 제약은 admin/stats 같은 메트릭 조회 인터페이스에 국한된 얘기였고, 로그 쪽은 AWS가 별도로 opt-in 기능(Envoy Access Log)을 제공한다
- 서비스의 `serviceConnectConfiguration`에 `logConfiguration`(로그 드라이버)과 `accessLogConfiguration`(포맷)을 함께 지정하면, Envoy가 요청 단위 메타데이터(HTTP 메서드·경로·응답코드·레이턴시 등)를 stdout으로 남긴다
- 로그 드라이버로 `awsfirelens`를 지정하면 별도 인프라 추가 없이 taskdef에 이미 떠 있는 FireLens 사이드카(4.3 참고)를 그대로 재사용해 Datadog으로 직행한다 — Envoy 프록시는 taskdef에 정의되지 않는 자동 주입 컨테이너지만, 같은 태스크 안의 컨테이너인 이상 로그 드라이버 지정 방식은 앱 컨테이너와 동일하게 작동한다

### 4.3 로그 수집

- Prerequisite
    - Docker 컨테이너 로그는 기본적으로 `stdout/stderr` 기반으로 동작하며, Docker 데몬이 이를 가로채 지정된 로그 드라이버로 전달한다
- 설정 방법
    - 로그 라우터 컨테이너에 `firelensConfiguration.type: fluentbit``지정, 애플리케이션 컨테이너에 logConfiguration.logDriver: awsfirelens 및 Datadog 목적지 옵션 지정, Task Role/Execution Role 권한 구성
- AWS FireLens 상세
    - ECS의 독립된 서비스가 아니라 ECS Task Definition 내에서 사용하는 로그 라우팅 기능이다
    - 실제 로그 처리는 사이드카로 실행되는 Fluent Bit(또는 Fluentd)가 담당하며, `awsfirelens`는 Docker 표준 드라이버가 아니라 ECS가 해석해 `fluentd` 드라이버로 치환하는 pseudo-driver다
    - 필요성
        - Fargate 환경에서는 사이드카 컨테이너가 사실상 유일한 로그 수집 경로이며, FireLens는 이 사이드카 구성(설정 파일 작성, 소켓 연결, 컨테이너 추가)을 Task Definition 내 최소 설정으로 자동화한다
- Datadog 권장 사항
    - Datadog 공식 문서는 ECS Fargate 로그 수집 방법으로 (1) `FireLens`방식과 (2) `awslogs`+CloudWatch+Lambda Forwarder 방식 두 가지를 제시하며, FireLens를 명시적으로 권장한다
    - 이유는 Fargate Task 내에서 Fluent Bit를 직접 구성할 수 있다는 점, Datadog Fluent Bit 출력 플러그인이 ECS Explorer에서 로그와 ECS 리소스를 연관 짓는 데 쓰이는 추가 태깅을 제공한다는 점이다
- Cf) Datadog Agent로는 불가한 이유 상세
    - Datadog Agent 표준 방식
        - Fargate에서 불가
        - `/var/run/docker.sock`으로 컨테이너 목록/메타데이터 조회
        - `/var/lib/docker/containers/<id>/<id>-json.log` 같은 호스트 파일시스템의 로그 파일을 직접 tail
        - 둘 다 호스트 마운트/소켓 접근이 전제인데, Fargate는 host를 아예 노출 안 하니 Agent 컨테이너도 예외 없이 막힘
    - Fluent Bit(FireLens) 방식
        - 파일을 읽는 게 아니라, `awsfirelens` 로그 드라이버 자체가 컨테이너 런타임 레벨에서 stdout/stderr를 소켓/네트워크 스트림으로 곧장 Fluent Bit 사이드카에 포워딩하는 구조
        - Docker의 네이티브 `fluentd` 로깅 드라이버와 같은 매커니즘이고, 이건 호스트 파일시스템이 아니라 컨테이너 런타임(런타임이 관리하는 로깅 파이프)이 처리해줍니다.
        - 즉 "로그 파일을 누가 읽느냐"가 아니라 "런타임이 로그 스트림을 어디로 쏴주느냐"의 문제라서, host 접근 없이 같은 task 안(같은 network namespace)에서만 통신하면 끝
        - 이건 Fargate 태스크 내부에 국한된 동작이라 AWS가 별도로 열어줄 필요도 없음
        - 그래서 Datadog 공식 문서에서도 ECS Fargate에서는 Agent의 기본 로그 수집 기능을 쓰지 말고 반드시 FireLens(Fluent Bit/Fluentd)로 우회하라고 명시

### 4.4 ECS 컨트롤 플레인 관측

- 3.3에서 제기한 제약(ECS가 배포·오케스트레이션 중 내부적으로 수행하는 판단·작업이 불투명한 문제)에 대한 self-service 해결책
- ECS Event Logs와 2026-07 AWS가 발표한 기능인 ECS Action Logs을 채택
    - Action Logs
        - ECS가 배포/managed daemon 갱신 중 수행한 내부 작업을 이벤트명·로그 레벨(INFO/WARN/ERROR)·리소스 ARN·상태 사유가 포함된 구조화 JSON으로 노출
        - 단 공식 문서상 "Service deployments"(배포 상태 전이/롤백/lifecycle hook)와 "Managed Daemon lifecycle"에만 한정되고, 스케일링·태스크 시작/중지·steady state 도달 같은 이벤트는 대상이 아님
    - Event Logs
        - EventBridge 기반
        - Action Logs가 커버하지 못하는 이벤트(`SERVICE_STEADY_STATE`, `TASKS_STOPPED`, `SERVICE_TASK_PLACEMENT_FAILURE`, `SERVICE_DISCOVERY_INSTANCE_UNHEALTHY` 등)를 EventBridge로 별도 수집
- Action Logs 활성화(클러스터 단위) → Kinesis Data Firehose → Datadog

### 4.5 APM/분산 트레이싱

- 앞선 제약들과 관련은 없다
- APM은 앱 컨테이너 내부의 dd-trace SDK가 2.2에서 설명한 awsvpc의 localhost 통신을 통해 같은 Task 내 Datadog Agent 사이드카로 트레이스를 전송하는 구조로, 표준 사이드카 패턴 안에서 정상 동작한다
- 본 프로젝트에서 APM을 다루는 이유는 관측성 공백을 메우기 위해서가 아니라, 5장 데모에서 Metric → APM → Log로 이어지는 트러블슈팅 흐름(Trace-to-Log)을 시연하기 위함
- 구현 방법
    - FastAPI 애플리케이션에 dd-trace 라이브러리 연동, 함수 실행 흐름 및 API 요청/응답 트레이스 수집

### 4.6 RUM

- 앞선 제약들과 관련은 없다
- 4.1~4.5는 전부 백엔드/인프라 관측이었고, 사용자가 실제로 겪는 브라우저 단(로딩 시간, 클릭, JS 에러)은 관측 공백으로 남아 있었다는 점에서 출발한 범위 확장이다
- 배치: 6번째 ECS 서비스(`frontend`, 정적 nginx)를 신설해 기존 서비스들과 같은 ALB 오리진으로 호스팅 — S3+CloudFront 구성이었다면 필요했을 CORS 설정이 이 구조에서는 불필요해진다
- SDK 연동: Datadog Browser RUM SDK를 CDN Sync 방식(`<head>`에서 스크립트 동기 로드 후 즉시 init)으로 연동
- 이 프로젝트가 빌드 파이프라인 없는 정적 파일 구조(`nginx:alpine`에 HTML/JS 복사만)이기 때문에, npm 패키지로 번들링하는 방식보다 기존 구조와 일관되고 Datadog이 공식적으로 권장하는 "앱 코드 실행 전에 최대한 먼저 로드" 원칙에도 부합한다
- APM 연결
    - `allowedTracingUrls`에 API 요청이 실제로 나가는 도메인(로컬은 별도 포트, ECS는 같은 오리진)을 등록해 dd-trace(4.4의 백엔드 APM)와 RUM 트레이스를 연결 — Browser → Gateway 체인이 하나의 트레이스로 시연됨

### 4.7 DB 레벨 관측

- 앞선 제약들과 관련은 없다
- 4.4(APM)가 서비스 간 호출 흐름은 보여주지만, 그 안에서 실행되는 개별 SQL 쿼리의 성능(실행 횟수, 평균 시간, 락 대기 등)은 여전히 블랙박스였다는 공백에서 출발한다
- 새 Agent를 추가하지 않고 Inventory에 이미 떠 있는 Datadog Agent 사이드카(4.1)를 그대로 재사용
- postgres 통합 설정만 추가
    - RDS는 컨테이너가 아님
        - 일반적인 Docker Autodiscovery는 "감시 대상 컨테이너에 라벨을 붙이면 Agent가 그 컨테이너를 찾아서 체크를 적용"하는 구조인데, RDS는 같은 Task 안에 떠 있는 컨테이너가 아니라 완전히 외부의 관리형 서비스라 라벨을 붙일 대상 자체가 없음
            - → 이것도 어떻게 보면, 호스트(DB가 설치되는 환경에 대한 접근 가능성이 없어서 발생하는 제약)
        - Agent 컨테이너 자기 자신에게 라벨을 붙이는 방식으로 이 공백을 우회한다(Datadog 공식 RDS DBM 설정 가이드가 제시하는 패턴)

### 4.8 대시보드 및 알림 설계

- 4.1~4.7에서 수집한 인프라/네트워크/로그/APM/ECS 컨트롤 플레인/RUM/DBM 데이터를 통합해 보여주는 Datadog 대시보드 구성
- 이상 상황 감지를 위한 Monitor(알림) 임계치 설계(예: 에러율 급증, 레이턴시 임계치 초과)

## 5. 데모 프로젝트

- 데모 프로젝트 명세(DEMO_PR_SPEC) 참조