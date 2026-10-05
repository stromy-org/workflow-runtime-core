-- WRC receipt capabilities v1; explicit setup only. No automatic role grants.
CREATE FUNCTION public.wrc_receipt_open_v1(args jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=6) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->5)='object' AND octet_length((args->5)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->5) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;

WITH opened AS (
    INSERT INTO public.delivery_receipts(service_namespace,destination,message_id,status,event_version)
    VALUES ((args->>0)::text,(args->>1)::text,(args->>2)::text,'pending',1)
    ON CONFLICT (service_namespace,destination,message_id) DO NOTHING RETURNING *
) INSERT INTO public.delivery_receipt_events (
    service_namespace,destination,message_id,event_version,transition,to_status,
    actor_kind,actor,reason,evidence
) SELECT service_namespace,destination,message_id,event_version,'open',status,
    (args->>3)::text,(args->>4)::text,'Receipt opened',(args->5)::jsonb FROM opened
;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_open_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_select_due_v1(args jsonb) RETURNS TABLE(message_id text)
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=5) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->2=args->3 AND (args->>4)::bigint BETWEEN 1 AND 1000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->2='null'::jsonb OR jsonb_typeof(args->2)='array') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF args->2<>'null'::jsonb THEN
IF (jsonb_array_length(args->2)<=1000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_array_elements(args->2) v WHERE jsonb_typeof(v)<>'string' OR length(btrim(v#>>'{}'))=0 OR octet_length(v#>>'{}')>2048) AND (SELECT count(*)=count(DISTINCT v) FROM jsonb_array_elements(args->2) v)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY SELECT r.message_id FROM public.delivery_receipts r
WHERE r.service_namespace=(args->>0)::text AND r.destination=(args->>1)::text AND r.status IN ('pending','failed')
    AND r.next_attempt_at<=now() AND ((CASE WHEN args->2='null'::jsonb THEN NULL ELSE ARRAY(SELECT jsonb_array_elements_text(args->2)) END)::text[] IS NULL OR r.message_id=ANY((CASE WHEN args->3='null'::jsonb THEN NULL ELSE ARRAY(SELECT jsonb_array_elements_text(args->3)) END)::text[]))
ORDER BY r.next_attempt_at,r.message_id LIMIT (args->>4)::bigint FOR UPDATE SKIP LOCKED;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_select_due_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_select_stale_v1(args jsonb) RETURNS TABLE(destination text,message_id text)
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=2) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>1)::bigint BETWEEN 1 AND 1000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY SELECT r.destination,r.message_id FROM public.delivery_receipts r
WHERE r.service_namespace=(args->>0)::text AND r.status='sending' AND r.lease_expires_at<=now()
ORDER BY r.lease_expires_at,r.message_id LIMIT (args->>1)::bigint FOR UPDATE SKIP LOCKED;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_select_stale_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_lease_expired_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=9) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>4='lease_expired') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>5='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->6)='string' AND length(btrim(args->>6))>0 AND octet_length(args->>6)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->7)='string' AND length(btrim(args->>7))>0 AND octet_length(args->>7)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='object' AND octet_length((args->8)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->8) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6='receipt-reconciler') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status='uncertain', lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>3)::text,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_expires_at<=now())
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>4)::text,
                prior_status,status,(args->>5)::text,(args->>6)::text,(args->>7)::text,(args->8)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_lease_expired_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_claim_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=10) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>5='claim') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->7)='string' AND length(btrim(args->>7))>0 AND octet_length(args->>7)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='string' AND length(btrim(args->>8))>0 AND octet_length(args->>8)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='object' AND octet_length((args->9)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->9) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3=args->>7 AND (args->>4)::double precision BETWEEN 0 AND 86400) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status='sending', attempts=r.attempts+1, claim_epoch=r.claim_epoch+1, lease_owner=(args->>3)::text, lease_expires_at=now()+make_interval(secs=>(args->>4)::double precision),
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status IN ('pending','failed') AND p.provider_ref IS NULL)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>5)::text,
                prior_status,status,(args->>6)::text,(args->>7)::text,(args->>8)::text,(args->9)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_claim_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_renew_lease_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=11) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6='renew_lease') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='string' AND length(btrim(args->>8))>0 AND octet_length(args->>8)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='object' AND octet_length((args->10)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->10) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8=args->>4 AND (args->>5)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>3)::double precision>0 AND (args->>3)::double precision<=86400) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET lease_expires_at=now()+make_interval(secs=>(args->>3)::double precision),
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>4)::text AND p.claim_epoch=(args->>5)::bigint AND p.lease_expires_at>now() AND (TRUE))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>6)::text,
                prior_status,status,(args->>7)::text,(args->>8)::text,(args->>9)::text,(args->10)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_renew_lease_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_start_effect_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=10) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>5='start_effect') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->7)='string' AND length(btrim(args->>7))>0 AND octet_length(args->>7)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='string' AND length(btrim(args->>8))>0 AND octet_length(args->>8)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='object' AND octet_length((args->9)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->9) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7=args->>3 AND (args->>4)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET effect_started_at=now(),
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>3)::text AND p.claim_epoch=(args->>4)::bigint AND p.lease_expires_at>now() AND (p.effect_started_at IS NULL AND p.provider_ref IS NULL))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>5)::text,
                prior_status,status,(args->>6)::text,(args->>7)::text,(args->>8)::text,(args->9)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_start_effect_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_record_provider_ref_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=12) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='record_provider_ref') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='string' AND length(btrim(args->>10))>0 AND octet_length(args->>10)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->11)='object' AND octet_length((args->11)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->11) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>9=args->>4 AND (args->>5)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->3)='string' AND length(btrim(args->>3))>0 AND octet_length(args->>3)<=2000 AND args->3=args->6) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET provider_ref=(args->>3)::text, provider_ref_recorded_at=COALESCE(r.provider_ref_recorded_at,now()),
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>4)::text AND p.claim_epoch=(args->>5)::bigint AND p.lease_expires_at>now() AND (p.effect_started_at IS NOT NULL AND (p.provider_ref IS NULL OR p.provider_ref=(args->>6)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>7)::text,
                prior_status,status,(args->>8)::text,(args->>9)::text,(args->>10)::text,(args->11)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_record_provider_ref_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_delivered_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=19) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='delivered') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='string' AND length(btrim(args->>17))>0 AND octet_length(args->>17)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->18)='object' AND octet_length((args->18)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->18) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>16=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='delivered' AND args->>8='delivered') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->12=args->4 AND args->13=args->4 AND args->11=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->6='null'::jsonb) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((p.effect_started_at IS NOT NULL AND (p.provider_ref IS NOT NULL OR (args->>11)::text::text IS NULL)) AND (p.provider_ref IS NULL OR (args->>12)::text::text IS NULL OR p.provider_ref=(args->>13)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>14)::text,
                prior_status,status,(args->>15)::text,(args->>16)::text,(args->>17)::text,(args->18)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_delivered_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_failed_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=18) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>13='failed') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->15)='string' AND length(btrim(args->>15))>0 AND octet_length(args->>15)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='object' AND octet_length((args->17)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->17) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='failed' AND args->>8='failed') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->11=args->4 AND args->12=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6=args->>16) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->17->>'outcome'='definitive_no_effect' AND length(btrim(args->17->>'adapter_code'))>0) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((p.provider_ref IS NULL) AND (p.provider_ref IS NULL OR (args->>11)::text::text IS NULL OR p.provider_ref=(args->>12)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>13)::text,
                prior_status,status,(args->>14)::text,(args->>15)::text,(args->>16)::text,(args->17)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_failed_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_uncertain_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=18) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>13='uncertain') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->15)='string' AND length(btrim(args->>15))>0 AND octet_length(args->>15)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='object' AND octet_length((args->17)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->17) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='uncertain' AND args->>8='uncertain') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->11=args->4 AND args->12=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6=args->>16) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((TRUE) AND (p.provider_ref IS NULL OR (args->>11)::text::text IS NULL OR p.provider_ref=(args->>12)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>13)::text,
                prior_status,status,(args->>14)::text,(args->>15)::text,(args->>16)::text,(args->17)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_uncertain_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_needs_review_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=18) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>13='needs_review') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->15)='string' AND length(btrim(args->>15))>0 AND octet_length(args->>15)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='object' AND octet_length((args->17)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->17) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='needs_review' AND args->>8='needs_review') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->11=args->4 AND args->12=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6=args->>16) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((TRUE) AND (p.provider_ref IS NULL OR (args->>11)::text::text IS NULL OR p.provider_ref=(args->>12)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>13)::text,
                prior_status,status,(args->>14)::text,(args->>15)::text,(args->>16)::text,(args->17)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_needs_review_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_blocked_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=18) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>13='blocked') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->15)='string' AND length(btrim(args->>15))>0 AND octet_length(args->>15)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='object' AND octet_length((args->17)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->17) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='blocked' AND args->>8='blocked') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->11=args->4 AND args->12=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6=args->>16) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((p.effect_started_at IS NULL AND p.provider_ref IS NULL) AND (p.provider_ref IS NULL OR (args->>11)::text::text IS NULL OR p.provider_ref=(args->>12)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>13)::text,
                prior_status,status,(args->>14)::text,(args->>15)::text,(args->>16)::text,(args->17)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_blocked_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_pending_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=18) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>13='pending') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>14='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->15)='string' AND length(btrim(args->>15))>0 AND octet_length(args->>15)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->16)='string' AND length(btrim(args->>16))>0 AND octet_length(args->>16)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->17)='object' AND octet_length((args->17)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->17) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>15=args->>9 AND (args->>10)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='pending' AND args->>8='pending') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4=args->4 AND args->5=args->4 AND args->11=args->4 AND args->12=args->4) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->4='null'::jsonb OR (jsonb_typeof(args->4)='string' AND length(btrim(args->>4))>0 AND octet_length(args->>4)<=2000)) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision BETWEEN 0 AND 3600) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>7)::double precision>0) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6=args->>16) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text, provider_ref=COALESCE(r.provider_ref,(args->>4)::text), provider_ref_recorded_at=CASE WHEN COALESCE(r.provider_ref,(args->>5)::text) IS NOT NULL THEN COALESCE(r.provider_ref_recorded_at,now()) ELSE NULL END, lease_owner=NULL, lease_expires_at=NULL, last_error=(args->>6)::text, next_attempt_at=now()+make_interval(secs=>(args->>7)::double precision), effect_started_at=CASE WHEN (args->>8)::text='failed' THEN NULL ELSE r.effect_started_at END,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='sending' AND p.lease_owner=(args->>9)::text AND p.claim_epoch=(args->>10)::bigint AND p.lease_expires_at>now() AND ((p.effect_started_at IS NULL AND p.provider_ref IS NULL) AND (p.provider_ref IS NULL OR (args->>11)::text::text IS NULL OR p.provider_ref=(args->>12)::text)))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>13)::text,
                prior_status,status,(args->>14)::text,(args->>15)::text,(args->>16)::text,(args->17)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_pending_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_block_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=9) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>4='block') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>5 IN ('service','human')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->6)='string' AND length(btrim(args->>6))>0 AND octet_length(args->>6)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->7)='string' AND length(btrim(args->>7))>0 AND octet_length(args->>7)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='object' AND octet_length((args->8)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->8) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status='blocked',last_error=(args->>3)::text,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status IN ('pending','failed'))
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>4)::text,
                prior_status,status,(args->>5)::text,(args->>6)::text,(args->>7)::text,(args->8)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_block_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_review_uncertain_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=10) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>5='review_uncertain') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>6='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->7)='string' AND length(btrim(args->>7))>0 AND octet_length(args->>7)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->8)='string' AND length(btrim(args->>8))>0 AND octet_length(args->>8)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='object' AND octet_length((args->9)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->9) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF ((args->>4)::bigint>=1 AND length(btrim(args->9->>'evidence_ref'))>0) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status='needs_review',last_error=(args->>3)::text,lease_owner=NULL,lease_expires_at=NULL,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='uncertain' AND p.event_version=(args->>4)::bigint)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>5)::text,
                prior_status,status,(args->>6)::text,(args->>7)::text,(args->>8)::text,(args->9)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_review_uncertain_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_reconcile_known_ref_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=12) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='reconcile_known_ref') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8='service') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='string' AND length(btrim(args->>10))>0 AND octet_length(args->>10)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->11)='object' AND octet_length((args->11)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->11) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->6)='string' AND length(btrim(args->>6))>0 AND octet_length(args->>6)<=2000 AND (args->>5)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (length(btrim(args->11->>'evidence_ref'))>0 AND length(btrim(args->11->>'expected_digest'))>0 AND length(btrim(args->11->>'observed_digest'))>0) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3=CASE WHEN args->11->>'expected_digest'=args->11->>'observed_digest' THEN 'delivered' ELSE 'needs_review' END) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text,last_error=(args->>4)::text,lease_owner=NULL,lease_expires_at=NULL,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status='uncertain' AND p.event_version=(args->>5)::bigint AND p.provider_ref=(args->>6)::text)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>7)::text,
                prior_status,status,(args->>8)::text,(args->>9)::text,(args->>10)::text,(args->11)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_reconcile_known_ref_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_resolve_delivered_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=12) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='resolve:delivered') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8 IN ('service','human')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='string' AND length(btrim(args->>10))>0 AND octet_length(args->>10)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->11)='object' AND octet_length((args->11)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->11) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='delivered' AND args->>5 IN ('uncertain','needs_review') AND (args->>6)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text,last_error=(args->>4)::text,lease_owner=NULL,lease_expires_at=NULL,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status=(args->>5)::text AND p.event_version=(args->>6)::bigint)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>7)::text,
                prior_status,status,(args->>8)::text,(args->>9)::text,(args->>10)::text,(args->11)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_resolve_delivered_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_resolve_blocked_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=12) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='resolve:blocked') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8 IN ('service','human')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='string' AND length(btrim(args->>10))>0 AND octet_length(args->>10)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->11)='object' AND octet_length((args->11)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->11) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='blocked' AND args->>5 IN ('uncertain','needs_review') AND (args->>6)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text,last_error=(args->>4)::text,lease_owner=NULL,lease_expires_at=NULL,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status=(args->>5)::text AND p.event_version=(args->>6)::bigint)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>7)::text,
                prior_status,status,(args->>8)::text,(args->>9)::text,(args->>10)::text,(args->11)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_resolve_blocked_v1(jsonb) FROM PUBLIC;

CREATE FUNCTION public.wrc_receipt_resolve_retry_new_generation_v1(args jsonb) RETURNS SETOF public.delivery_receipts
LANGUAGE plpgsql SECURITY DEFINER STRICT SET search_path=pg_catalog,public AS $wrc$
BEGIN
IF (jsonb_typeof(args)='array' AND jsonb_array_length(args)=12) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>7='resolve:retry_new_generation') IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>8 IN ('service','human')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->9)='string' AND length(btrim(args->>9))>0 AND octet_length(args->>9)<=256) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->10)='string' AND length(btrim(args->>10))>0 AND octet_length(args->>10)<=2000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->11)='object' AND octet_length((args->11)::text)<=4000) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (NOT EXISTS (SELECT 1 FROM jsonb_each(args->11) e WHERE e.key NOT IN ('evidence_ref','expected_digest','observed_digest','outcome','adapter_code') OR jsonb_typeof(e.value)<>'string')) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (args->>3='blocked' AND args->>5 IN ('uncertain','needs_review') AND (args->>6)::bigint>=1) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->0)='string' AND length(btrim(args->>0))>0 AND octet_length(args->>0)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->1)='string' AND length(btrim(args->>1))>0 AND octet_length(args->>1)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
IF (jsonb_typeof(args->2)='string' AND length(btrim(args->>2))>0 AND octet_length(args->>2)<=2048) IS NOT TRUE THEN
    RAISE EXCEPTION 'invalid receipt capability arguments' USING ERRCODE='22023';
END IF;
RETURN QUERY 
        WITH prior AS MATERIALIZED (
            SELECT * FROM public.delivery_receipts
            WHERE service_namespace=(args->>0)::text AND destination=(args->>1)::text AND message_id=(args->>2)::text FOR UPDATE
        ), changed AS (
            UPDATE public.delivery_receipts r SET status=(args->>3)::text,last_error=(args->>4)::text,lease_owner=NULL,lease_expires_at=NULL,
                event_version=r.event_version+1, updated_at=now()
            FROM prior p WHERE r.service_namespace=p.service_namespace AND r.destination=p.destination
                AND r.message_id=p.message_id AND (p.status=(args->>5)::text AND p.event_version=(args->>6)::bigint)
            RETURNING r.*, p.status AS prior_status
        ), audit AS (
            INSERT INTO public.delivery_receipt_events (
                service_namespace,destination,message_id,event_version,transition,
                from_status,to_status,actor_kind,actor,reason,evidence
            ) SELECT service_namespace,destination,message_id,event_version,(args->>7)::text,
                prior_status,status,(args->>8)::text,(args->>9)::text,(args->>10)::text,(args->11)::jsonb FROM changed RETURNING event_id
        ) SELECT (jsonb_populate_record(NULL::public.delivery_receipts,to_jsonb(changed))).* FROM changed CROSS JOIN audit
        ;
END;
$wrc$;
REVOKE ALL ON FUNCTION public.wrc_receipt_resolve_retry_new_generation_v1(jsonb) FROM PUBLIC;
